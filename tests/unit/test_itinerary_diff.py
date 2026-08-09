"""What changed, computed from two persisted states rather than from prose."""

from datetime import date, datetime

from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary
from app.models.lock import LockRecord
from app.services.itinerary_diff import changed_days, diff_days
from tests.conftest import sample_state

DAY = date(2026, 10, 3)


def item(item_id: str, title: str, hour: int, *, kind: str = "activity") -> ItineraryItem:
    return ItineraryItem(
        item_id=item_id,
        type=kind,
        title=title,
        start_at=datetime(2026, 10, 3, hour, 0),
        end_at=datetime(2026, 10, 3, hour + 1, 0),
    )


def trip_with(*items: ItineraryItem, locks: list[str] | None = None):
    state = sample_state()
    state.itinerary = TripItinerary(days=[ItineraryDay(date=DAY, items=list(items))])
    state.locks = [
        LockRecord(target_kind="itinerary_item", target_id=item_id, reason="user asked")
        for item_id in (locks or [])
    ]
    return state


def test_an_untouched_day_reports_nothing_changed():
    before = trip_with(item("a", "Museum", 10))
    after = trip_with(item("a", "Museum", 10))

    diff = diff_days(before, after)[0]

    assert not diff.touched
    assert [c.item_id for c in diff.unchanged] == ["a"]
    assert diff.summary() == "2026-10-03: nothing changed"
    assert changed_days(before, after) == []


def test_each_kind_of_change_is_classified():
    before = trip_with(item("a", "Museum", 10), item("b", "Plantation", 14))
    after = trip_with(item("a", "Museum", 10), item("c", "Beach", 15))

    diff = diff_days(before, after)[0]

    assert [c.item_id for c in diff.added] == ["c"]
    assert [c.item_id for c in diff.removed] == ["b"]
    assert [c.item_id for c in diff.unchanged] == ["a"]
    assert diff.moved == []
    assert diff.touched


def test_a_retimed_item_is_a_move_and_keeps_where_it_came_from():
    before = trip_with(item("a", "Makena Landing", 14))
    after = trip_with(item("a", "Makena Landing", 15))

    moved = diff_days(before, after)[0].moved[0]

    assert moved.kind == "moved"
    assert moved.from_time == "14:00"
    assert moved.at == "15:00"


def test_a_locked_item_is_flagged_even_when_nothing_happened_to_it():
    """That a replan left dinner alone is the point of asking."""
    before = trip_with(item("a", "Museum", 10), item("d", "Dinner", 19), locks=["d"])
    after = trip_with(item("d", "Dinner", 19), locks=["d"])

    diff = diff_days(before, after)[0]

    assert [c.item_id for c in diff.removed] == ["a"]
    dinner = diff.unchanged[0]
    assert dinner.item_id == "d"
    assert dinner.locked is True
    assert "1 locked and untouched" in diff.summary()


def test_a_day_that_did_not_exist_before_is_all_additions():
    before = sample_state()
    before.itinerary = TripItinerary()
    after = trip_with(item("a", "Museum", 10))

    diff = diff_days(before, after)[0]

    assert len(diff.added) == 1
    assert diff.removed == []


def test_a_deleted_day_is_all_removals():
    before = trip_with(item("a", "Museum", 10))
    after = sample_state()
    after.itinerary = TripItinerary()

    diff = diff_days(before, after)[0]

    assert [c.item_id for c in diff.removed] == ["a"]
    assert diff.added == []


def test_only_touched_days_survive_the_filter():
    before = sample_state()
    before.itinerary = TripItinerary(
        days=[
            ItineraryDay(date=DAY, items=[item("a", "Museum", 10)]),
            ItineraryDay(date=date(2026, 10, 4), items=[item("b", "Beach", 11)]),
        ]
    )
    after = before.model_copy(deep=True)
    after.itinerary.days[1].items = []

    touched = changed_days(before, after)

    assert [d.day for d in touched] == [date(2026, 10, 4)]


def test_changes_are_ordered_by_time_so_the_summary_reads_like_a_day():
    before = trip_with()
    after = trip_with(item("late", "Dinner", 19), item("early", "Coffee", 9))

    added = diff_days(before, after)[0].added

    assert [c.title for c in added] == ["Coffee", "Dinner"]
