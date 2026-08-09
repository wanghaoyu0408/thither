"""What a day costs, and what the figure is allowed to claim.

The failure this guards against is one-directional: an unmeasured leg counted as
zero minutes makes a hard day look easy, and nobody notices until they are
standing in the car park. So every total comes with how much of the day is
behind it, and a partial is never printed as a total.
"""

from datetime import date, datetime

from app.models.common import Money
from app.models.entity import PlaceEntity
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary
from app.models.lock import LockRecord
from app.models.trip import TripState
from app.models.validation import ValidationIssue
from app.services.day_metrics import describe, summarize_day
from app.services.validation_service import TravelLookup, long_haul_mode, mode_between

DAY = date(2026, 8, 10)

# Far enough apart that nothing is walkable between them.
FAR = [(20.63, -156.44), (20.89, -156.47), (20.76, -156.45)]


def place(entity_id: str, lat: float, lng: float) -> PlaceEntity:
    return PlaceEntity(entity_id=entity_id, name=entity_id, lat=lat, lng=lng)


def item(item_id: str, entity_id: str, hour: int, cost: float | None = None) -> ItineraryItem:
    return ItineraryItem(
        item_id=item_id,
        type="activity",
        entity_id=entity_id,
        title=entity_id,
        start_at=datetime(2026, 8, 10, hour, 0),
        end_at=datetime(2026, 8, 10, hour + 1, 0),
        estimated_cost=Money(amount=cost) if cost is not None else None,
    )


def driving_trip(*, stops: int = 3, costs: list[float | None] | None = None) -> TripState:
    state = TripState.new(title="Maui")
    state.brief.scope.rental_car = "already_arranged"
    costs = costs or [None] * stops
    items = []
    for index in range(stops):
        lat, lng = FAR[index % len(FAR)]
        state.entities[f"ent_{index}"] = place(f"ent_{index}", lat, lng)
        items.append(item(f"item_{index}", f"ent_{index}", 9 + index * 2, costs[index]))
    state.itinerary = TripItinerary(days=[ItineraryDay(date=DAY, items=items)])
    return state


def day_of(state: TripState) -> ItineraryDay:
    return state.itinerary.days[0]


# --- the mode a trip actually travels in --------------------------------------


def test_a_trip_with_a_car_drives_rather_than_assuming_transit():
    """Google publishes no transit for Maui. Assuming it made every leg unknown
    and silently totalled the day's travel at zero."""
    driving = driving_trip()
    transit = driving_trip()
    transit.brief.scope.rental_car = "not_needed"

    assert long_haul_mode(driving) == "driving"
    assert long_haul_mode(transit) == "transit"

    far_a, far_b = driving.entities["ent_0"], driving.entities["ent_1"]
    assert mode_between(driving, far_a, far_b) == "driving"
    assert mode_between(transit, far_a, far_b) == "transit"


def test_a_short_hop_is_still_walked_whatever_the_trip_drives():
    state = driving_trip()
    near = place("ent_near", 20.631, -156.441)

    assert mode_between(state, state.entities["ent_0"], near) == "walking"


# --- measured, and how much of it ---------------------------------------------


def test_a_fully_measured_day_reports_its_load():
    state = driving_trip()
    travel = TravelLookup(
        minutes={
            ("ent_0", "ent_1", "driving"): 32.0,
            ("ent_1", "ent_2", "driving"): 26.0,
        }
    )

    summary = summarize_day(day_of(state), state, travel)

    assert summary.stops == 3
    assert summary.driving_minutes == 58.0
    assert summary.legs_measured == 2 and summary.legs_total == 2
    assert summary.travel_is_partial is False
    assert "58 min driving" in describe(summary)


def test_an_unmeasured_leg_is_counted_as_missing_not_as_zero():
    state = driving_trip()
    travel = TravelLookup(minutes={("ent_0", "ent_1", "driving"): 32.0})

    summary = summarize_day(day_of(state), state, travel)

    assert summary.driving_minutes == 32.0, "the measured leg only"
    assert summary.legs_measured == 1 and summary.legs_total == 2
    assert summary.travel_is_partial is True
    # And the label says so, so 32 minutes is not read as the day's driving.
    assert "32 min driving (partial)" in describe(summary)


def test_a_day_with_no_measurements_says_so_rather_than_reporting_nothing():
    state = driving_trip()

    summary = summarize_day(day_of(state), state, TravelLookup())

    assert summary.driving_minutes is None
    assert summary.legs_total == 2 and summary.legs_measured == 0
    assert "travel times not measured" in describe(summary)


def test_walking_distance_and_time_are_both_reported():
    state = driving_trip(stops=2)
    state.entities["ent_1"] = place("ent_1", 20.6355, -156.4455)  # a few hundred metres
    travel = TravelLookup(
        minutes={("ent_0", "ent_1", "walking"): 9.0},
        meters={("ent_0", "ent_1", "walking"): 720.0},
    )

    summary = summarize_day(day_of(state), state, travel)

    assert summary.walking_minutes == 9.0
    assert summary.total_walking_km == 0.72
    assert summary.driving_minutes is None


# --- money --------------------------------------------------------------------


def test_a_cost_covering_every_item_is_a_total():
    state = driving_trip(stops=2, costs=[95.0, 70.0])
    travel = TravelLookup(minutes={("ent_0", "ent_1", "driving"): 20.0})

    summary = summarize_day(day_of(state), state, travel)

    assert summary.estimated_cost.amount == 165.0
    assert summary.cost_is_partial is False
    assert "165 USD total" in describe(summary)


def test_a_cost_covering_some_items_is_a_partial_estimate():
    state = driving_trip(stops=3, costs=[95.0, 70.0, None])

    summary = summarize_day(day_of(state), state)

    assert summary.estimated_cost.amount == 165.0
    assert summary.items_priced == 2 and summary.items_total == 3
    assert summary.cost_is_partial is True
    # Never "165 USD total" while a third of the day carries no price.
    assert "~165 USD partial estimate" in describe(summary)
    assert not any("total" in line for line in describe(summary))


def test_two_currencies_are_not_added_together():
    state = driving_trip(stops=2, costs=[95.0, 70.0])
    day_of(state).items[1].estimated_cost = Money(amount=70.0, currency="EUR")

    summary = summarize_day(day_of(state), state)

    assert summary.estimated_cost is None, "adding them would invent an exchange rate"
    assert "2 currencies" in summary.notes


# --- the rest of the day ------------------------------------------------------


async def test_the_fetcher_asks_for_the_mode_the_validator_reads():
    """They disagreed for three milestones. `_ensure_routes` fetched walking;
    `mode_between` looked up transit above 1.5 km. Every long leg was therefore
    unknown by construction, and the day's transit total was always zero."""
    from app.agent.tool_registry import ToolContext, _ensure_routes
    from app.config import Settings
    from app.models.itinerary_plan import PlannedDay, PlannedItem
    from app.models.route import RouteLeg
    from app.models.tool import ToolResult
    from app.services.proposal_store import ProposalStore

    asked: list[str] = []

    class RecordingRoutes:
        async def get_routes(self, spec, *, entities):
            asked.append(spec.mode)
            return ToolResult[RouteLeg](
                source="google_routes",
                results=[
                    RouteLeg(
                        origin_index=0,
                        destination_index=0,
                        mode=spec.mode,
                        duration_seconds=1920,
                        distance_meters=24000,
                        status="ok",
                    )
                ],
            )

    class Toolbox:
        routes = RecordingRoutes()

    state = driving_trip(stops=2)
    context = ToolContext(
        state=state, toolbox=Toolbox(), proposals=ProposalStore(), settings=Settings()
    )

    class Proposal:
        days = [
            PlannedDay(
                date=DAY,
                items=[
                    PlannedItem(item_id="item_0", title="a", type="activity", entity_id="ent_0"),
                    PlannedItem(item_id="item_1", title="b", type="activity", entity_id="ent_1"),
                ],
            )
        ]

    await _ensure_routes(context, Proposal())

    assert asked == ["driving"], "a car trip must be measured by car"
    # And the key it stored is the one the validator will look up.
    assert ("ent_0", "ent_1", "driving") in context.travel.minutes
    summary = summarize_day(day_of(state), state, context.travel)
    assert summary.driving_minutes == 32.0
    assert summary.travel_is_partial is False


def test_locked_items_and_open_issues_are_counted():
    state = driving_trip()
    state.locks.append(
        LockRecord(target_kind="itinerary_item", target_id="item_1", reason="booked")
    )
    issues = [
        ValidationIssue(severity="error", type="closed_at_visit_time", item_ids=["item_2"],
                        message="shut"),
        ValidationIssue(severity="info", type="hours_unknown", item_ids=["item_0"],
                        message="unknown"),
    ]

    summary = summarize_day(day_of(state), state, issues=issues)

    assert summary.locked_items == 1
    assert summary.unresolved_issues == 1, "info is not an unresolved issue"
    assert summary.pace == "balanced"
    assert summary.measured_at is not None
