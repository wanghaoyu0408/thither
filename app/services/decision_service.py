"""The decisions of a trip, as something a person can look at.

`TripDecisions` is a good store and a poor view. It says nothing about which
decisions this trip actually has to make, which are settled and which are merely
unstarted, or which are locked - lock state lives in `state.locks`, not on the
decision, and the `DecisionStatus = "locked"` value is dead and must not be read.

This module answers those questions and nothing else. It is pure and read-only:
it never fetches, never mutates, and never invents an option. A decision this
trip does not need is reported as hidden *with the reason*, because "we did not
ask" and "we asked and found nothing" are different answers and a blank space
tells them apart badly.
"""

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from app.models.decision import SINGLETON_DECISIONS, Decision, DecisionOption
from app.models.trip import TripState
from app.services.conflict_service import unresolved_blocking
from app.services.option_metrics import Metric, metrics_for, unscored_for

if TYPE_CHECKING:
    from app.services.calibration_service import Calibrations

# What each slot is called on screen, and the order they are worked through.
TITLES: dict[str, str] = {
    "destination": "Destination",
    "departure_airport": "Departure airport",
    "arrival_airport": "Arrival airport",
    "flights": "Flights",
    "hotel_area": "Neighbourhood",
    "hotel": "Hotel",
}

DecisionKind = Literal[
    "destination",
    "departure_airport",
    "arrival_airport",
    "flights",
    "hotel_area",
    "hotel",
    "places",
]


class OptionView(BaseModel):
    option_id: str
    label: str
    status: str
    selected: bool
    score: float | None = None
    coverage: float | None = None
    group_total: float | None = None
    group_is_split: bool = False
    pros: list[str] = []
    cons: list[str] = []
    evidence_count: int = 0

    # The figures that make this a comparison rather than a ranked list. Same
    # builder the explanation uses, so a card and its `Why?` cannot disagree.
    metrics: list[Metric] = []
    unscored: list[str] = []


class DecisionView(BaseModel):
    """One decision, as the workspace shows it."""

    name: str
    kind: DecisionKind
    title: str

    # Present at all? A slot this trip needs but has not researched is still
    # listed, with no options - that a decision is outstanding is information.
    exists: bool = False
    status: str = "researching"

    booked: bool = False
    booked_reference: str | None = None

    locked: bool = False
    lock_id: str | None = None

    stale_reason: str | None = None
    rationale: str | None = None

    selected: OptionView | None = None
    options: list[OptionView] = []
    # Kept apart, never mixed back into the ranking. A rejected option
    # reappearing in a shortlist is the complaint this separation exists for.
    rejected: list[OptionView] = []

    needs_verification: bool = False

    relevant: bool = True
    hidden_reason: str | None = None


def label_for(data: Any) -> str:
    """A name a person would recognise, from whatever payload a slot holds."""
    # Airports first, and by name rather than by city. The loop below would
    # return at `city`, so both Chicago airports rendered as "Chicago" - two
    # identical cards, and the same word twice in the model's own summary,
    # since `_selected_label` reads this too. It is the flight branch's defect
    # one attribute away: "a shortlist of four flights renders as the same
    # line four times". Only AirportOption carries `iata`.
    iata = getattr(data, "iata", None)
    if iata:
        name = getattr(data, "name", None)
        return f"{name} ({iata})" if name else str(iata)

    for attribute in ("area_name", "city", "name"):
        value = getattr(data, attribute, None)
        if value:
            return str(value)
    origin = getattr(data, "origin", None)
    destination = getattr(data, "destination", None)
    if origin and destination:
        airlines = getattr(data, "airlines", []) or []
        parts = [f"{origin} → {destination}"]
        if airlines:
            parts.append(airlines[0])
        # The departure is what tells two offers on the same route apart. Without
        # it a shortlist of four flights renders as the same line four times.
        departure = getattr(data, "departure_at", None)
        if departure is not None:
            parts.append(departure.strftime("%H:%M"))
        return " · ".join(parts)
    return getattr(data, "entity_id", None) or "option"


def _option_view(
    option: DecisionOption,
    *,
    selected_id: str | None,
    calibrations: "Calibrations | None" = None,
) -> OptionView:
    return OptionView(
        option_id=option.option_id,
        label=label_for(option.data),
        status=option.status,
        selected=option.option_id == selected_id,
        # Score and coverage stay separate all the way to the screen. Blending
        # them here would hide how much evidence a number rests on, which is the
        # one thing the reader needs to judge it.
        score=option.score.total if option.score else None,
        coverage=option.score.coverage if option.score else None,
        group_total=option.group_score.total if option.group_score else None,
        group_is_split=bool(option.group_score and option.group_score.is_split),
        pros=option.pros,
        cons=option.cons,
        evidence_count=len(option.evidence_refs),
        metrics=metrics_for(option.data, calibrations),
        unscored=unscored_for(option.data),
    )


def _lock_for(state: TripState, decision: Decision) -> tuple[bool, str | None]:
    """Lock state comes from `state.locks`, never from `Decision.status`.

    `DecisionStatus` includes "locked" and nothing has ever set it. Reading it
    would report every locked decision as unlocked.
    """
    for lock in state.locks:
        if lock.target_kind == "decision" and lock.target_id == decision.decision_id:
            return True, lock.lock_id
    return False, None


def _rejected_ids(state: TripState) -> set[str]:
    return {
        record.target_id
        for record in state.rejections
        if record.target_kind in ("decision_option", "destination", "hotel_area")
    }


def _relevance(state: TripState, name: str) -> tuple[bool, str | None]:
    """Whether this trip has to make this decision at all.

    Erring toward showing: a decision hidden wrongly is invisible work, while a
    decision shown needlessly is only noise.
    """
    brief = state.brief
    if name == "destination":
        if brief.destination.city and not brief.destination.flexible:
            return False, f"{brief.destination.city} is fixed for this trip"
        return True, None
    if name in ("departure_airport", "arrival_airport", "flights"):
        if not (brief.origin.city or brief.origin.airport_codes):
            return False, "no origin set, so there is nothing to fly from"
        return True, None
    return True, None


def decision_views(
    state: TripState, calibrations: "Calibrations | None" = None
) -> list[DecisionView]:
    """Every decision this trip makes, settled or not, in working order.

    `calibrations` is how well this system's own figures are known. Passed
    in rather than looked up, because the corpus lives in a table and this
    module reads only the trip; absent, the cards say exactly what they
    said before M10.
    """
    stored = dict(state.decisions.iter_decisions())
    rejected_ids = _rejected_ids(state)
    blocked = {
        target for conflict in unresolved_blocking(state) for target in conflict.affects
    }

    views: list[DecisionView] = []
    for name in SINGLETON_DECISIONS:
        relevant, hidden_reason = _relevance(state, name)
        decision = stored.get(name)
        view = DecisionView(
            name=name,
            kind=name,  # type: ignore[arg-type]
            title=TITLES[name],
            relevant=relevant,
            hidden_reason=hidden_reason,
            needs_verification=name in blocked,
        )
        if decision is not None:
            _fill(view, state, decision, rejected_ids=rejected_ids, calibrations=calibrations)
        views.append(view)

    for key, decision in state.decisions.place_shortlists.items():
        view = DecisionView(
            name=f"place_shortlists.{key}",
            kind="places",
            title=decision.rationale or key.replace("_", " "),
            needs_verification=f"place_shortlists.{key}" in blocked,
        )
        _fill(view, state, decision, rejected_ids=rejected_ids, calibrations=calibrations)
        views.append(view)

    return views


def _fill(
    view: DecisionView,
    state: TripState,
    decision: Decision,
    *,
    rejected_ids: set[str],
    calibrations: "Calibrations | None" = None,
) -> None:
    view.exists = True
    view.status = decision.status
    view.booked = decision.booked
    view.booked_reference = decision.booked_reference
    view.stale_reason = decision.stale_reason
    view.rationale = decision.rationale
    view.locked, view.lock_id = _lock_for(state, decision)

    for option in decision.options:
        rendered = _option_view(
            option, selected_id=decision.selected_option_id, calibrations=calibrations
        )
        if option.status == "rejected" or option.option_id in rejected_ids:
            view.rejected.append(rendered)
            continue
        view.options.append(rendered)
        if rendered.selected:
            view.selected = rendered


def visible(views: list[DecisionView]) -> list[DecisionView]:
    """What the workspace lists: relevant decisions, plus any already worked on.

    A decision with stored options is shown even when the relevance rules would
    hide it - the work happened, and hiding it would lose it.
    """
    return [view for view in views if view.relevant or view.exists]
