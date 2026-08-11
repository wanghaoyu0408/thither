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

import re
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from app.models.common import utcnow
from app.models.intake import SCOPE_LABELS, SHOPPABLE, ClarificationQuestion
from app.models.trip import TripState

GapKind = Literal["dates", "scope", "origin"]


class Gap(BaseModel):
    """A missing fact, and why it stops us.

    `requirement_id` is the stable name a question has to cite. Matching on the
    JSON pointer instead meant guessing whether "/brief/party" overlapped
    "/brief/party/adults", and a prefix test that is wrong in either direction
    either drops a real question or lets an unnecessary one through.
    """

    requirement_id: str
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
                requirement_id="dates",
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
                    requirement_id=f"scope.{part}",
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
                requirement_id="origin",
                kind="origin",
                field="/brief/origin",
                why="flights cannot be searched without knowing where the trip starts",
            )
        )

    return gaps


# --- what day it is ----------------------------------------------------------


def today_at(state: TripState) -> date:
    """The current date the trip is planned against.

    Local at the destination when the trip knows its timezone, UTC otherwise.
    Read at call time from the clock - never stored, never written into the
    prompt - so a trip planned on one day and reopened on another resolves
    "8/10" against the right year both times.
    """
    zone = state.brief.timezone
    if zone:
        try:
            return datetime.now(ZoneInfo(zone)).date()
        except ZoneInfoNotFoundError:
            pass
    return utcnow().date()


_PARTIAL = re.compile(r"^\s*(\d{1,2})\s*[/-]\s*(\d{1,2})\s*$")


def resolve_date(value: str, today: date) -> date | None:
    """An ISO date, or a month/day with the year worked out from `today`.

    A date without a year is not ambiguous to a person - "8/10" said in August
    means the one coming up - but it was ambiguous enough to the model that it
    asked the traveller which year they meant, about a date two days away. The
    rule is fixed here so nobody has to ask: the next occurrence on or after
    today.
    """
    text = (value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    match = _PARTIAL.match(text)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None


def outstanding_questions(state: TripState) -> list[ClarificationQuestion]:
    """Unanswered questions whose gap is still open.

    A question is a means to close a gap, not a fact in its own right, and
    `ask_clarifications` will not let one be asked that does not name a gap. So
    when the gap closes by any route - the workspace form, an inline brief edit,
    or the traveller simply saying it in the chat and the agent recording it -
    the question is spent.

    Counting a spent question as pending is what left a real trip with no way
    forward: the lodging question was answered in conversation, the scope was
    written to the brief, the gap disappeared, and the question sat there
    unanswered forever, holding confirmation shut on something nobody was still
    waiting to hear.

    Spent is not answered. Nothing here writes `answered_at` - we do not know
    what they would have picked, only that it stopped mattering.
    """
    open_ids = {gap.requirement_id for gap in missing_blocking(state)}
    return [
        question
        for question in state.intake.unanswered
        if question.requirement_id in open_ids
    ]


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
    # A trip that already holds places or days had its brief settled the old
    # way. Only an outstanding question holds it now.
    #
    # This used to also require `status == "collecting"`, which meant recording
    # a single brief fact flipped the trip to `awaiting_confirmation` and gated
    # a trip that had been planning happily for weeks - the agent blocked itself
    # by writing something down.
    if has_research(state) and not outstanding_questions(state):
        return True
    return False


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
    if decision is None:
        return True
    # Three sources, three kinds of claim: the scope above is their standing
    # instruction, `booked` is a fact about the world, and `set_aside_reason`
    # is what a search turned up. Any one of them is enough to stop shopping.
    return not (decision.booked or decision.set_aside_reason)


def ready_to_confirm(state: TripState) -> bool:
    """Whether `POST /trips/{id}/intake/confirm` would accept it.

    Exactly the endpoint's own test, and deliberately nothing more. It used to
    also require no unanswered questions - a stricter rule than the server
    enforced, which meant the browser disabled a button the backend was ready to
    honour. A readiness flag that does not predict the thing it gates is worse
    than no flag.
    """
    return not missing_blocking(state)


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
        questions=outstanding_questions(state),
        confirmed_at=(
            state.intake.confirmed_at.isoformat() if state.intake.confirmed_at else None
        ),
        confirmed_revision=state.intake.confirmed_revision,
    )
