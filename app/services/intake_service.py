"""What we still need to know, and whether research may start.

The agent used to begin researching on its first turn, because nothing stopped
it: every research handler's only preconditions were credentials and a per-turn
budget. So "我和老婆想去 Maui 玩几天" went straight to searching restaurants
without knowing when, for how many people, or whether the flights were booked.

This module decides two things and nothing else:

  * which missing facts genuinely block planning - deterministically, so the
    model cannot talk itself past them, and narrowly, so a traveller who has
    already said everything is asked nothing;
  * whether research is allowed yet.

It is pure and read-only. It never fetches, never mutates, and never fills a gap
with a plausible value - an unknown party size stays unknown here, and the
flight search refuses rather than pricing a trip for an invented passenger.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.models.intake import SCOPE_LABELS, SHOPPABLE, ClarificationQuestion
from app.models.trip import TripState

GapKind = Literal["dates", "scope", "origin"]


class Gap(BaseModel):
    """A missing fact, and why it stops us."""

    kind: GapKind
    field: str
    why: str


class IntakeView(BaseModel):
    """The brief as a person reads it, for confirmation."""

    status: str
    research_allowed: bool
    ready_to_confirm: bool

    headline: str = ""
    travelers: str | None = None

    already_arranged: list[str] = []
    will_plan: list[str] = []
    will_not_search: list[str] = []

    priorities: list[str] = []
    preferences: list[str] = []

    gaps: list[Gap] = []
    questions: list[ClarificationQuestion] = []

    confirmed_at: str | None = None
    confirmed_revision: int | None = None


def missing_blocking(state: TripState) -> list[Gap]:
    """The facts without which planning would be guesswork.

    Deliberately short. Everything else - budget, party size, interests - either
    has a defensible default or is asked for by the tool that actually needs it,
    at the point it needs it. A question asked before it matters is an
    interruption, not diligence.
    """
    brief = state.brief
    gaps: list[Gap] = []

    # When. Either decided dates, or a window plus a length: "four days in
    # October" is a real answer and must not be forced into invented dates.
    if not (brief.dates.decided or brief.dates.bounded):
        gaps.append(
            Gap(
                kind="dates",
                field="/brief/dates",
                why="nothing can be scheduled, priced or checked for opening hours without dates",
            )
        )

    # What to do about the two parts that cost a provider call to shop for.
    # Assuming either way is expensive: search for a flight somebody already
    # holds, or produce a plan with no way to get there.
    for part in SHOPPABLE:
        if brief.scope.state_of(part) == "unknown":
            gaps.append(
                Gap(
                    kind="scope",
                    field=f"/brief/scope/{part}",
                    why=f"we do not know whether to look for {SCOPE_LABELS[part].lower()}",
                )
            )

    # Where from - but only when we are actually shopping for flights. A trip
    # with tickets already bought does not need an origin to plan the days.
    if brief.scope.state_of("flights") == "plan" and not (
        brief.origin.city or brief.origin.airport_codes
    ):
        gaps.append(
            Gap(
                kind="origin",
                field="/brief/origin",
                why="flights cannot be searched without knowing where the trip starts",
            )
        )

    return gaps


def has_research(state: TripState) -> bool:
    return bool(state.entities or state.itinerary.days)


def research_allowed(state: TripState) -> bool:
    """Whether broad research may run.

    Confirmed briefs may. So may trips that predate intake entirely: a trip
    already carrying places or days had its brief settled the old way, and
    locking that work behind a confirmation nobody was ever asked for would be
    a migration dressed up as a rule.
    """
    if state.intake.status == "confirmed":
        return True
    untouched_by_intake = state.intake.status == "collecting" and not state.intake.questions
    return untouched_by_intake and has_research(state)


def party_known(state: TripState) -> bool:
    """Whether we can quote a per-person price without inventing a passenger."""
    return state.brief.party.adults is not None


def should_shop_for(state: TripState, part: str) -> bool:
    """Whether to go looking for flights or lodging.

    One place decides, because two sources of truth for the same rule is how
    "already booked" gets honoured in one code path and ignored in another.
    Scope is the traveller's instruction; `booked` is the record of a specific
    arrangement. Either one is enough to stop shopping.
    """
    if state.brief.scope.state_of(part) in ("already_arranged", "not_needed"):
        return False
    decision_name = "flights" if part == "flights" else "hotel"
    decision = getattr(state.decisions, decision_name, None)
    return not (decision is not None and decision.booked)


def ready_to_confirm(state: TripState) -> bool:
    return not missing_blocking(state) and not state.intake.unanswered


# --- the summary a person confirms -------------------------------------------


def _day(value: date) -> str:
    """"Aug 10". %-d is not portable to Windows, so strip the zero by hand."""
    return value.strftime("%b %d").replace(" 0", " ")


def _headline(state: TripState) -> str:
    brief = state.brief
    where = brief.destination.city or brief.destination.region or brief.destination.country
    if not where:
        where = "Destination undecided"
    dates = brief.dates
    if dates.decided:
        when = f"{_day(dates.start)}–{_day(dates.end)}"
    elif dates.bounded:
        nights = dates.duration_nights
        window = dates.earliest or dates.latest
        when = f"{nights} nights"
        if window:
            when += f", around {window.strftime('%B')}"
    else:
        when = "dates not set"
    return f"{where} · {when}"


def _travelers_line(state: TripState) -> str | None:
    party = state.brief.party
    if party.adults is None:
        # Said plainly rather than shown as a number nobody gave.
        return None
    people = f"{party.adults} adult" + ("s" if party.adults != 1 else "")
    if party.children:
        people += f", {party.children} child" + ("ren" if party.children != 1 else "")
    return people


def _preference_lines(state: TripState) -> list[str]:
    """Preferences already known, so intake never asks for them again."""
    lines: list[str] = []
    lines.append(f"{state.brief.pace.capitalize()} pace")
    seen: set[str] = set()
    for traveler in state.travelers:
        preferences = traveler.preferences
        if preferences is None:
            continue
        for restriction in preferences.food.dietary_restrictions:
            if restriction not in seen:
                seen.add(restriction)
                lines.append(f"{restriction} ({traveler.name})")
        if preferences.pace.preferred_start_time:
            note = f"Prefers to start around {preferences.pace.preferred_start_time}"
            if note not in lines:
                lines.append(note)
    return lines


def intake_view(state: TripState) -> IntakeView:
    """Everything the confirmation screen shows, derived, never stored."""
    scope = state.brief.scope
    gaps = missing_blocking(state)

    return IntakeView(
        status=state.intake.status,
        research_allowed=research_allowed(state),
        ready_to_confirm=ready_to_confirm(state),
        headline=_headline(state),
        travelers=_travelers_line(state),
        already_arranged=[SCOPE_LABELS[part] for part in scope.parts("already_arranged")],
        will_plan=[SCOPE_LABELS[part] for part in scope.parts("plan")],
        # Named explicitly. "I will not search for flights" is a commitment, and
        # a commitment the traveller cannot see is one they cannot correct.
        will_not_search=[
            SCOPE_LABELS[part]
            for part in SHOPPABLE
            if scope.state_of(part) in ("already_arranged", "not_needed")
        ],
        priorities=list(state.brief.priorities),
        preferences=_preference_lines(state),
        gaps=gaps,
        questions=state.intake.unanswered,
        confirmed_at=(
            state.intake.confirmed_at.isoformat() if state.intake.confirmed_at else None
        ),
        confirmed_revision=state.intake.confirmed_revision,
    )
