"""What this trip is waiting for next, derived from what it holds.

Three places already answered a version of this question and disagreed with
each other: `SINGLETON_DECISIONS` (the order integrity walks decisions in),
`CHOICE_ORDER` in the browser (the order cards are shown in), and fifteen
lines of JavaScript inside the progress strip. None of them encoded a
dependency, so nothing could say "the hotel is waiting on the neighbourhood"
- and a traveller who chose their airport and their area watched planning
stop dead, because no code anywhere knew that flights and hotels were now
unblocked.

Written in the mould of `intake_service.missing_blocking`: pure, read-only,
deliberately short, and phrased for a person. It answers *what is waiting*,
never *what will happen* - the agent decides that, and the traveller can
always ask for something else entirely.

The dependencies here are not invented. Each one already exists as an
enforced rule somewhere, and this module is where they are finally written
down together:

  * hotel_area -> hotel   `hotel_service.resolve_area` refuses outright
  * departure_airport -> flights   ...which nothing enforces yet. Stated here
    so the gap is at least visible; see the note on `_FLIGHTS_NEED_AIRPORT`.
"""

from typing import Literal

from pydantic import BaseModel

from app.models.trip import TripState
from app.services.intake_service import research_allowed, should_shop_for

StepKind = Literal["choose", "research", "generate"]

# The airport decision exists to be chosen from, but `search_flights` takes its
# origins from the model and never reads the selection - so unlike the hotel
# area, nothing refuses a flight search that ignores it. Naming the dependency
# here does not enforce it; it makes "the traveller chose an airport" visible
# to the projection the model reads, which is as far as this module can go.
_FLIGHTS_NEED_AIRPORT = ("departure_airport",)


class NextStep(BaseModel):
    """One thing the trip is waiting for.

    `blocked_by` is empty for everything returned - a step with an unsettled
    dependency is not a next step, it is a later one, and reporting it would
    invite doing things in an order the tools will refuse.
    """

    kind: StepKind
    # The decision name, or "itinerary". Machine-readable, for the browser.
    what: str
    # One sentence, for a person.
    label: str


def _decision(state: TripState, name: str):
    return getattr(state.decisions, name, None)


def _is_settled(state: TripState, name: str) -> bool:
    """Chosen, booked, or deliberately out of scope - anything but waiting."""
    decision = _decision(state, name)
    if decision is None:
        return False
    return bool(decision.selected_option_id) or decision.booked


def _awaiting_choice(state: TripState, name: str) -> bool:
    """Options are on the table and nobody has picked one."""
    decision = _decision(state, name)
    if decision is None or decision.booked or decision.selected_option_id:
        return False
    return len([o for o in decision.options if o.status != "rejected"]) >= 2


def _needs_airports(state: TripState) -> bool:
    """Only a trip that knows where it starts can compare airports."""
    origin = state.brief.origin
    return bool(origin.city or origin.airport_codes)


def next_steps(state: TripState) -> list[NextStep]:
    """Everything the trip is waiting for, nearest first.

    Empty means there is nothing obvious left - which is a real answer, not a
    failure: a trip whose flights are already booked and whose days are laid
    out is finished, and saying "next: nothing" is how the screen stops
    pretending otherwise.
    """
    if not research_allowed(state):
        # The brief owns this stage. Offering planning steps beside an
        # unconfirmed brief would be two things asking for attention at once.
        return []

    steps: list[NextStep] = []

    # Choices first: something already on the table beats starting new work.
    for name, label in (
        ("destination", "Choose where to go"),
        ("departure_airport", "Choose which airport to fly from"),
        ("arrival_airport", "Choose which airport to fly into"),
        ("flights", "Choose a flight"),
        ("hotel_area", "Choose a neighbourhood"),
        ("hotel", "Choose a hotel"),
    ):
        if _awaiting_choice(state, name):
            steps.append(NextStep(kind="choose", what=name, label=label))

    shopping_flights = should_shop_for(state, "flights")
    shopping_lodging = should_shop_for(state, "lodging")

    # Then research that nothing is blocking.
    if shopping_flights and _needs_airports(state):
        for name, label in (
            ("departure_airport", "Compare airports to fly from"),
            ("arrival_airport", "Compare airports to fly into"),
        ):
            if _decision(state, name) is None:
                steps.append(NextStep(kind="research", what=name, label=label))

    if shopping_flights and _decision(state, "flights") is None:
        blocked = [
            name
            for name in _FLIGHTS_NEED_AIRPORT
            if _decision(state, name) is not None and not _is_settled(state, name)
        ]
        if not blocked:
            steps.append(NextStep(kind="research", what="flights", label="Find flight options"))

    if shopping_lodging:
        if _decision(state, "hotel_area") is None:
            steps.append(
                NextStep(kind="research", what="hotel_area", label="Compare neighbourhoods")
            )
        elif _decision(state, "hotel") is None and _is_settled(state, "hotel_area"):
            # The order hotel_service.resolve_area enforces: an area, then a hotel.
            steps.append(NextStep(kind="research", what="hotel", label="Find a place to stay"))

    if state.entities and not state.itinerary.days:
        steps.append(
            NextStep(kind="generate", what="itinerary", label="Build the daily itinerary")
        )

    return steps


def waiting_on_the_agent(state: TripState) -> list[NextStep]:
    """The steps the agent could take right now, unprompted.

    A `choose` step is waiting on the traveller and must never be read as work
    the agent should do for them.
    """
    return [step for step in next_steps(state) if step.kind != "choose"]
