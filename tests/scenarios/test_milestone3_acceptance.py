"""Milestone 3 acceptance, proved without an LLM.

    "Plan 5 days in Tokyo."  ->  "Day 3 is too busy. Make it easier."
    Only Day 3 should change.

The conversational path is exercised separately in tests/live. Proving the
mechanism deterministically here means a model having an off day cannot make
the milestone look broken - and means these tests run offline, free, forever.
"""

from datetime import date, datetime, time, timedelta

import pytest

from app.models import (
    ItineraryDay,
    ItineraryItem,
    LockRecord,
    PatchOperation,
    PlaceEntity,
    TripPatch,
)
from app.models.itinerary_plan import PlanParams, ReplanParams
from app.models.patch import PatchScope
from app.services import apply_patch
from app.services.itinerary_service import build_itinerary, replan_day
from app.services.validation_service import validate_itinerary
from tests.conftest import sample_state

TRIP_START = date(2026, 10, 3)
DAY_THREE = date(2026, 10, 5)

# Open every day 09:00-23:00, so scheduling is never blocked by hours here.
WIDE_HOURS = {
    "periods": [
        {"open": {"day": d, "hour": 9, "minute": 0}, "close": {"day": d, "hour": 23, "minute": 0}}
        for d in range(7)
    ]
}


def make_place(index: int, kind: str, *, lat: float, lng: float) -> PlaceEntity:
    categories = {"meal": ["restaurant"], "cafe": ["cafe"], "activity": ["museum"]}[kind]
    return PlaceEntity(
        entity_id=f"ent_{kind}_{index:02d}",
        provider_refs={"google_place_id": f"ChIJ_{kind}_{index:02d}"},
        name=f"{kind.title()} {index}",
        categories=categories,
        lat=lat,
        lng=lng,
        rating=4.0 + (index % 9) / 10,
        rating_count=200 + index * 17,
        opening_hours=WIDE_HOURS,
        timezone="Asia/Tokyo",
    )


def planning_state():
    """A Tokyo trip with five neighbourhoods' worth of real-shaped candidates."""
    state = sample_state()
    state.brief.dates.start = TRIP_START
    state.brief.dates.end = TRIP_START + timedelta(days=4)
    state.brief.timezone = "Asia/Tokyo"
    state.brief.pace = "balanced"
    state.itinerary.days = []
    state.entities = {}

    # Five clusters, far enough apart that clustering has something to find.
    for cluster, (lat, lng) in enumerate(
        [
            (35.659, 139.700),
            (35.714, 139.796),
            (35.671, 139.765),
            (35.690, 139.700),
            (35.644, 139.699),
        ]
    ):
        for offset, kind in enumerate(["meal", "meal", "cafe", "activity", "activity"]):
            index = cluster * 10 + offset
            entity = make_place(index, kind, lat=lat + offset * 0.002, lng=lng + offset * 0.002)
            state.entities[entity.entity_id] = entity
    return state


def busy_day_state():
    """Five planned days where day 3 is deliberately overloaded."""
    state = planning_state()

    days = []
    for offset in range(5):
        day_date = TRIP_START + timedelta(days=offset)
        cluster = offset
        count = 7 if day_date == DAY_THREE else 3
        items = []
        for position in range(count):
            entity_id = (
                f"ent_{['meal', 'meal', 'cafe', 'activity', 'activity'][position % 5]}_"
                f"{cluster * 10 + position % 5:02d}"
            )
            start = datetime.combine(day_date, time(10, 0)) + timedelta(hours=position * 2)
            items.append(
                ItineraryItem(
                    item_id=f"item_{offset}_{position}",
                    type="restaurant" if position % 5 < 3 else "activity",
                    entity_id=entity_id,
                    title=state.entities[entity_id].name,
                    start_at=start,
                    end_at=start + timedelta(minutes=75),
                )
            )
        days.append(ItineraryDay(date=day_date, theme=f"Area {offset + 1}", items=items))

    state.itinerary.days = days
    return state


# --- "Plan 5 days in Tokyo." -------------------------------------------------


def test_planning_produces_five_days():
    proposal = build_itinerary(planning_state(), params=PlanParams(days=5))

    assert len(proposal.days) == 5
    assert proposal.days_changed == [TRIP_START + timedelta(days=i) for i in range(5)]
    assert not proposal.is_empty


def test_a_generated_itinerary_has_no_validation_errors():
    state = planning_state()
    proposal = build_itinerary(state, params=PlanParams(days=5))

    errors = [issue for issue in proposal.validation.issues if issue.severity == "error"]
    assert errors == [], [issue.message for issue in errors]


def test_generation_never_schedules_a_place_it_can_see_is_shut():
    """The builder leaves a slot empty rather than book somewhere closed.

    Note the hours used: published periods that simply never cover daytime.
    An *empty* periods list would mean "unknown", which is deliberately not the
    same thing and must not be filtered.
    """
    state = planning_state()
    graveyard = {
        "periods": [
            {
                "open": {"day": d, "hour": 2, "minute": 0},
                "close": {"day": d, "hour": 4, "minute": 0},
            }
            for d in range(7)
        ]
    }
    for entity in state.entities.values():
        if "cafe" in entity.categories:
            entity.opening_hours = graveyard

    proposal = build_itinerary(state, params=PlanParams(days=5))

    scheduled = {item.entity_id for day in proposal.days for item in day.items}
    assert not any(entity_id and "cafe" in entity_id for entity_id in scheduled)


def test_a_place_with_no_published_hours_is_still_schedulable():
    """Unknown is not closed - refusing to schedule it would lose half of Tokyo."""
    state = planning_state()
    for entity in state.entities.values():
        if "cafe" in entity.categories:
            entity.opening_hours = None

    proposal = build_itinerary(state, params=PlanParams(days=5))

    scheduled = {item.entity_id for day in proposal.days for item in day.items}
    assert any(entity_id and "cafe" in entity_id for entity_id in scheduled)


def test_days_are_geographically_coherent():
    state = planning_state()
    proposal = build_itinerary(state, params=PlanParams(days=5))

    from app.services.geo import haversine_km

    for day in proposal.days:
        points = [
            (state.entities[item.entity_id].lat, state.entities[item.entity_id].lng)
            for item in day.items
            if item.entity_id in state.entities
        ]
        for first in points:
            for second in points:
                assert haversine_km(*first, *second) < 6.0, f"{day.date} sprawls"


def test_a_generated_itinerary_applies_cleanly():
    state = planning_state()
    proposal = build_itinerary(state, params=PlanParams(days=5))

    result = apply_patch(
        state,
        TripPatch(
            base_revision=state.revision, reason="plan 5 days", operations=proposal.operations
        ),
    )

    assert result.applied is True
    assert len(result.state.itinerary.days) == 5


def test_planning_without_places_says_so_rather_than_inventing():
    state = planning_state()
    state.entities = {}

    proposal = build_itinerary(state, params=PlanParams(days=5))

    assert proposal.is_empty
    assert "No places are stored" in proposal.summary


# --- "Day 3 is too busy. Make it easier." ------------------------------------


def test_replanning_day_three_reduces_it():
    state = busy_day_state()
    before = len(state.itinerary.days[2].items)

    proposal = replan_day(state, DAY_THREE, params=ReplanParams(intensity="relaxed"))

    assert before == 7
    assert len(proposal.days[0].items) < before
    assert proposal.days_changed == [DAY_THREE]


def test_the_replan_is_scoped_to_day_three():
    proposal = replan_day(busy_day_state(), DAY_THREE, params=ReplanParams(intensity="relaxed"))

    assert proposal.scope == PatchScope(kind="itinerary_day", target_id="2026-10-05")


def test_only_day_three_changes():
    """The acceptance criterion, checked day by day."""
    state = busy_day_state()
    before = {day.date: day.model_dump(mode="json") for day in state.itinerary.days}

    proposal = replan_day(state, DAY_THREE, params=ReplanParams(intensity="relaxed"))
    result = apply_patch(
        state,
        TripPatch(
            base_revision=state.revision,
            reason="day 3 is too busy",
            operations=proposal.operations,
            scope=proposal.scope,
        ),
    )

    assert result.applied is True, [e.message for e in result.errors]

    after = {day.date: day.model_dump(mode="json") for day in result.state.itinerary.days}
    assert set(before) == set(after)

    changed = [day_date for day_date in before if before[day_date] != after[day_date]]
    assert changed == [DAY_THREE]


def test_a_locked_item_survives_the_replan_byte_for_byte():
    state = busy_day_state()
    dinner = state.itinerary.days[2].items[-1]
    state.locks = [
        LockRecord(
            lock_id="lock_dinner",
            target_kind="itinerary_item",
            target_id=dinner.item_id,
            reason="reservation already made",
        )
    ]

    proposal = replan_day(state, DAY_THREE, params=ReplanParams(intensity="relaxed"))
    result = apply_patch(
        state,
        TripPatch(
            base_revision=state.revision,
            reason="day 3 is too busy, keep dinner",
            operations=proposal.operations,
            scope=proposal.scope,
        ),
    )

    assert result.applied is True, [e.message for e in result.errors]

    survivors = [
        item
        for day in result.state.itinerary.days
        for item in day.items
        if item.item_id == dinner.item_id
    ]
    assert len(survivors) == 1
    assert survivors[0].model_dump(mode="json") == dinner.model_dump(mode="json")


def test_the_replan_leaves_no_validation_errors():
    state = busy_day_state()
    proposal = replan_day(state, DAY_THREE, params=ReplanParams(intensity="relaxed"))

    errors = [issue for issue in proposal.validation.issues if issue.severity == "error"]
    assert errors == [], [issue.message for issue in errors]


def test_replanning_a_day_that_does_not_exist_reports_rather_than_guesses():
    proposal = replan_day(busy_day_state(), date(2027, 1, 1))

    assert proposal.is_empty
    assert "not in this itinerary" in proposal.summary


def test_an_explicit_drop_of_a_locked_item_is_refused_with_a_reason():
    state = busy_day_state()
    dinner = state.itinerary.days[2].items[-1]
    state.locks = [
        LockRecord(
            lock_id="lock_dinner",
            target_kind="itinerary_item",
            target_id=dinner.item_id,
            reason="booked",
        )
    ]

    proposal = replan_day(state, DAY_THREE, params=ReplanParams(drop_item_ids=[dinner.item_id]))

    assert any("refused to drop locked items" in warning for warning in proposal.warnings)
    assert any(item.item_id == dinner.item_id for item in proposal.days[0].items)


# --- the guard rail itself ---------------------------------------------------


def test_a_patch_that_reaches_outside_its_scope_is_rejected():
    """Scope is enforced by the server, not merely intended by the caller."""
    state = busy_day_state()

    result = apply_patch(
        state,
        TripPatch(
            base_revision=state.revision,
            reason="claims to touch day 3 only",
            scope=PatchScope(kind="itinerary_day", target_id="2026-10-05"),
            operations=[
                PatchOperation(op="set", path="/itinerary/days/2/theme", value="Quieter"),
                # Day 1 is out of bounds.
                PatchOperation(op="set", path="/itinerary/days/0/theme", value="Sneaky"),
            ],
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["SCOPE_VIOLATION"]
    assert "2026-10-03 changed" in result.errors[0].message
    assert state.revision == 0


def test_scope_permits_changing_the_targeted_day():
    state = busy_day_state()

    result = apply_patch(
        state,
        TripPatch(
            base_revision=state.revision,
            reason="rename day 3",
            scope=PatchScope(kind="itinerary_day", target_id="2026-10-05"),
            operations=[PatchOperation(op="set", path="/itinerary/days/2/theme", value="Quieter")],
        ),
    )

    assert result.applied is True
    assert result.state.itinerary.days[2].theme == "Quieter"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("/brief/pace", "packed"),
        ("/travelers/0/name", "Someone Else"),
        ("/status", "ready"),
    ],
)
def test_scope_freezes_everything_outside_the_itinerary(path, value):
    state = busy_day_state()

    result = apply_patch(
        state,
        TripPatch(
            base_revision=state.revision,
            reason="wandering outside scope",
            scope=PatchScope(kind="itinerary_day", target_id="2026-10-05"),
            operations=[PatchOperation(op="set", path=path, value=value)],
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["SCOPE_VIOLATION"]


def test_scope_allows_adding_a_place_the_replan_needs():
    """A replan may need somewhere the trip has never seen."""
    state = busy_day_state()
    newcomer = make_place(99, "cafe", lat=35.66, lng=139.70)

    result = apply_patch(
        state,
        TripPatch(
            base_revision=state.revision,
            reason="day 3 needs a new cafe",
            scope=PatchScope(kind="itinerary_day", target_id="2026-10-05"),
            operations=[
                PatchOperation(
                    op="add",
                    path=f"/entities/{newcomer.entity_id}",
                    value=newcomer.model_dump(mode="json"),
                )
            ],
        ),
    )

    assert result.applied is True


def test_scope_still_forbids_rewriting_an_existing_place():
    state = busy_day_state()

    result = apply_patch(
        state,
        TripPatch(
            base_revision=state.revision,
            reason="quietly re-rate a place",
            scope=PatchScope(kind="itinerary_day", target_id="2026-10-05"),
            operations=[PatchOperation(op="set", path="/entities/ent_meal_00/rating", value=1.0)],
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["SCOPE_VIOLATION"]


def test_scope_forbids_adding_or_removing_days():
    state = busy_day_state()

    result = apply_patch(
        state,
        TripPatch(
            base_revision=state.revision,
            reason="delete day 1 while claiming day 3",
            scope=PatchScope(kind="itinerary_day", target_id="2026-10-05"),
            operations=[PatchOperation(op="remove", path="/itinerary/days/0")],
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["SCOPE_VIOLATION"]


def test_scope_naming_a_day_that_does_not_exist_is_rejected():
    state = busy_day_state()

    result = apply_patch(
        state,
        TripPatch(
            base_revision=state.revision,
            reason="scope to nowhere",
            scope=PatchScope(kind="itinerary_day", target_id="2030-01-01"),
            operations=[PatchOperation(op="set", path="/itinerary/days/2/theme", value="x")],
        ),
    )

    assert result.applied is False
    assert "not in the itinerary" in result.errors[0].message


# --- validator ---------------------------------------------------------------


def test_the_validator_reports_but_does_not_repair():
    state = busy_day_state()
    snapshot = state.model_dump(mode="json")

    validate_itinerary(state)

    assert state.model_dump(mode="json") == snapshot


def test_validation_records_the_revision_it_ran_against():
    state = busy_day_state()

    result = validate_itinerary(state)

    assert result.validated_revision == state.revision
    assert result.validated_at is not None
