from datetime import date

from app.models import ItineraryDay, LockRecord, TripConstraint
from app.services import check_integrity
from tests.conftest import make_entity, make_item, sample_state


def test_complete_state_has_no_problems():
    assert check_integrity(sample_state()) == []


def test_dangling_entity_reference():
    state = sample_state()
    state.itinerary.days[0].items[0].entity_id = "ent_missing"

    problems = check_integrity(state)

    assert any("unknown entity 'ent_missing'" in p for p in problems)


def test_registry_key_must_match_entity_id():
    state = sample_state()
    state.entities["ent_wrong_key"] = make_entity("ent_real", "Somewhere")

    problems = check_integrity(state)

    assert any("holds entity_id 'ent_real'" in p for p in problems)


def test_duplicate_item_ids_detected():
    state = sample_state()
    state.itinerary.days.append(
        ItineraryDay(date=date(2026, 10, 4), items=[make_item("item_dinner")])
    )

    problems = check_integrity(state)

    assert any("duplicate itinerary item_id 'item_dinner'" in p for p in problems)


def test_selection_must_be_one_of_the_options():
    state = sample_state()
    state.decisions.destination.selected_option_id = "opt_ghost"

    problems = check_integrity(state)

    assert any("selects unknown option 'opt_ghost'" in p for p in problems)


def test_constraint_traveler_must_exist():
    state = sample_state()
    state.constraints.append(
        TripConstraint(
            id="con_1",
            category="food",
            description="no shellfish",
            type="hard",
            scope="traveler",
            traveler_id="trv_ghost",
            source="user_explicit",
        )
    )

    problems = check_integrity(state)

    assert any("unknown traveler 'trv_ghost'" in p for p in problems)


def test_lock_must_target_something_real():
    state = sample_state()
    state.locks = [
        LockRecord(
            lock_id="lock_x", target_kind="itinerary_item", target_id="item_ghost", reason="x"
        )
    ]

    problems = check_integrity(state)

    assert any("targets missing itinerary_item 'item_ghost'" in p for p in problems)


def test_day_outside_the_trip_dates():
    state = sample_state()
    state.itinerary.days.append(ItineraryDay(date=date(2026, 11, 20)))

    problems = check_integrity(state)

    assert any("after the trip end" in p for p in problems)


def test_duplicate_day_dates():
    state = sample_state()
    state.itinerary.days.append(ItineraryDay(date=date(2026, 10, 3)))

    problems = check_integrity(state)

    assert any("duplicate itinerary day 2026-10-03" in p for p in problems)
