"""What actually changed between two persisted trips.

The agent's own account of a replan is prose, and prose is where "I made day 3
easier" can quietly mean "I also moved your dinner". This computes the change
from the two states themselves, so the summary the user reads is derived from
what landed rather than from what was intended.

Deliberately takes *before* and *after* `TripState` rather than an
`ItineraryProposal`. A proposal describes an intention; only the reloaded state
describes a fact, and after the hardening pass every mutation path has one to
hand (INVARIANTS.md section 3).
"""

from datetime import date, datetime

from pydantic import BaseModel

from app.models.itinerary import ItineraryItem
from app.models.trip import TripState

ChangeKind = str  # "added" | "removed" | "moved" | "unchanged"


class ItemChange(BaseModel):
    """One item's fate across a change."""

    kind: ChangeKind

    item_id: str
    title: str
    type: str

    # Wall-clock, as the itinerary stores it. `from_time` is only set for moves.
    at: str | None = None
    from_time: str | None = None

    # Locked items appear in the diff even when nothing happened to them: that a
    # replan left them alone is the reassurance the user is looking for.
    locked: bool = False


class DayDiff(BaseModel):
    day: date

    added: list[ItemChange] = []
    removed: list[ItemChange] = []
    moved: list[ItemChange] = []
    unchanged: list[ItemChange] = []

    @property
    def touched(self) -> bool:
        return bool(self.added or self.removed or self.moved)

    def summary(self) -> str:
        """One line, in the terms a person would use."""
        if not self.touched:
            return f"{self.day}: nothing changed"
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        if self.moved:
            parts.append(f"{len(self.moved)} moved")
        locked = sum(1 for item in self.unchanged if item.locked)
        if locked:
            parts.append(f"{locked} locked and untouched")
        return f"{self.day}: " + ", ".join(parts)


def _clock(value: datetime | None) -> str | None:
    return value.strftime("%H:%M") if value else None


def _locked_item_ids(state: TripState) -> set[str]:
    return {
        lock.target_id for lock in state.locks if lock.target_kind == "itinerary_item"
    }


def _change(item: ItineraryItem, kind: ChangeKind, locked: bool, **extra) -> ItemChange:
    return ItemChange(
        kind=kind,
        item_id=item.item_id,
        title=item.title,
        type=item.type,
        at=_clock(item.start_at),
        locked=locked,
        **extra,
    )


def diff_days(before: TripState, after: TripState) -> list[DayDiff]:
    """Per-day added / removed / moved / unchanged, most recently touched first.

    Items are matched on `item_id`, which is why the patch engine treats ids as
    stable: a replan that reused ids for different places would make this report
    a set of moves rather than a set of replacements, and the user would be told
    something false in a very calm voice.
    """
    locked_before = _locked_item_ids(before)
    locked_after = _locked_item_ids(after)

    old_days = {day.date: day for day in before.itinerary.days}
    new_days = {day.date: day for day in after.itinerary.days}

    diffs: list[DayDiff] = []
    for when in sorted(set(old_days) | set(new_days)):
        old_day, new_day = old_days.get(when), new_days.get(when)
        old_items = {item.item_id: item for item in (old_day.items if old_day else [])}
        new_items = {item.item_id: item for item in (new_day.items if new_day else [])}

        diff = DayDiff(day=when)

        for item_id, item in new_items.items():
            locked = item_id in locked_after
            previous = old_items.get(item_id)
            if previous is None:
                diff.added.append(_change(item, "added", locked))
            elif previous.start_at != item.start_at:
                diff.moved.append(
                    _change(item, "moved", locked, from_time=_clock(previous.start_at))
                )
            else:
                diff.unchanged.append(_change(item, "unchanged", locked))

        for item_id, item in old_items.items():
            if item_id not in new_items:
                diff.removed.append(_change(item, "removed", item_id in locked_before))

        for bucket in (diff.added, diff.removed, diff.moved, diff.unchanged):
            bucket.sort(key=lambda change: (change.at or "", change.title))

        diffs.append(diff)

    return diffs


def changed_days(before: TripState, after: TripState) -> list[DayDiff]:
    """Only the days something actually happened to."""
    return [diff for diff in diff_days(before, after) if diff.touched]
