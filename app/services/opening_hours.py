"""Reading Google's opening hours correctly.

Deceptively fiddly, and the real Tokyo data we already stored contains every
trap at once:

    shibuya zetton
      Sunday   16:00-23:00
      Monday   11:30-15:00  AND  17:00 -> day 2, 00:00

1. Several periods can share a day - the lunch/dinner split is the norm in
   Japan, not an edge case.
2. A period can cross midnight: `close.day` may be the day after `open.day`,
   and an izakaya closing at 00:00 is ordinary.
3. A 24-hour venue is a single `open` point with **no** `close` field.
4. `openNow` is a snapshot from whenever the place was fetched. It is sitting
   in our database saying `false` about an August afternoon; consulting it when
   checking a Friday evening in October would silently mark half the shortlist
   shut. This module reads `periods` and never touches it.
5. No hours at all means *unknown*, never *closed*. Plenty of small venues
   publish none, and telling a user a place is shut because Google was quiet is
   exactly the invention the whole project is built to avoid.

Times are naive local wall-clock at the place, which is the frame Google
publishes them in.
"""

from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Any

MINUTES_PER_DAY = 24 * 60
DAYS_PER_WEEK = 7
WEEK_MINUTES = DAYS_PER_WEEK * MINUTES_PER_DAY


class OpenState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    # No published hours. Distinct from CLOSED, and must stay that way.
    UNKNOWN = "unknown"


class Interval:
    """A half-open [start, end) window in minutes-since-Sunday-00:00.

    Weekly minutes rather than wall-clock so a period crossing midnight - or
    Saturday into Sunday - is a plain numeric range instead of a special case.
    """

    __slots__ = ("start", "end")

    def __init__(self, start: int, end: int) -> None:
        self.start = start
        self.end = end

    def contains(self, minute: int) -> bool:
        return self.start <= minute < self.end

    def __repr__(self) -> str:
        return f"Interval({self.start}, {self.end})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Interval) and other.start == self.start and other.end == self.end


def week_minute(moment: datetime) -> int:
    """Minutes since Sunday 00:00, matching Google's day numbering."""
    # Python: Monday is 0. Google: Sunday is 0.
    google_day = (moment.weekday() + 1) % DAYS_PER_WEEK
    return google_day * MINUTES_PER_DAY + moment.hour * 60 + moment.minute


def _point_minute(point: dict[str, Any]) -> int:
    return (
        int(point.get("day", 0)) * MINUTES_PER_DAY
        + int(point.get("hour", 0)) * 60
        + int(point.get("minute", 0))
    )


def parse_periods(opening_hours: dict[str, Any] | None) -> list[Interval] | None:
    """Google `regularOpeningHours` -> weekly intervals.

    Returns None when there is nothing to go on, which callers must treat as
    unknown rather than closed. Never reads `openNow`.
    """
    if not opening_hours:
        return None

    periods = opening_hours.get("periods")
    if not periods:
        return None

    intervals: list[Interval] = []
    for period in periods:
        opens = period.get("open")
        if not opens:
            continue

        closes = period.get("close")
        if closes is None:
            # Open with no close means always open, all week.
            return [Interval(0, WEEK_MINUTES)]

        start = _point_minute(opens)
        end = _point_minute(closes)

        if end <= start:
            # Wrapped past the end of the week, e.g. Saturday 22:00 -> Sunday 02:00.
            intervals.append(Interval(start, WEEK_MINUTES))
            intervals.append(Interval(0, end))
        else:
            intervals.append(Interval(start, end))

    return intervals or None


def state_at(opening_hours: dict[str, Any] | None, moment: datetime) -> OpenState:
    """Is the place open at this local wall-clock time?"""
    intervals = parse_periods(opening_hours)
    if intervals is None:
        return OpenState.UNKNOWN

    minute = week_minute(moment)
    return OpenState.OPEN if any(i.contains(minute) for i in intervals) else OpenState.CLOSED


def covers_visit(
    opening_hours: dict[str, Any] | None,
    start_at: datetime,
    end_at: datetime | None = None,
) -> OpenState:
    """Is the place open for the whole visit, not merely at the moment of arrival?

    Arriving ten minutes before closing is not the same as being open, and this
    is the check the itinerary validator actually wants.
    """
    if end_at is None or end_at <= start_at:
        return state_at(opening_hours, start_at)

    intervals = parse_periods(opening_hours)
    if intervals is None:
        return OpenState.UNKNOWN

    # Sample the visit at both ends and on each minute boundary a period could
    # fall on. Stepping by a minute would be exact but wasteful; the only way
    # to be open at both ends yet shut between is a gap, so period edges are
    # the points that matter.
    moment = start_at
    checkpoints = [start_at, end_at - timedelta(minutes=1)]
    while moment < end_at:
        checkpoints.append(moment)
        moment += timedelta(minutes=30)

    minutes = {week_minute(point) for point in checkpoints}
    return (
        OpenState.OPEN
        if all(any(i.contains(m) for i in intervals) for m in minutes)
        else OpenState.CLOSED
    )


def opens_on(opening_hours: dict[str, Any] | None, day: datetime) -> list[tuple[time, time]]:
    """Local open/close windows falling on this calendar day, for explanation."""
    intervals = parse_periods(opening_hours)
    if intervals is None:
        return []

    google_day = (day.weekday() + 1) % DAYS_PER_WEEK
    day_start = google_day * MINUTES_PER_DAY
    day_end = day_start + MINUTES_PER_DAY

    windows: list[tuple[time, time]] = []
    for interval in intervals:
        start = max(interval.start, day_start)
        end = min(interval.end, day_end)
        if start >= end:
            continue
        windows.append(
            (
                time((start % MINUTES_PER_DAY) // 60, (start % MINUTES_PER_DAY) % 60),
                time((end % MINUTES_PER_DAY) // 60 % 24, (end % MINUTES_PER_DAY) % 60),
            )
        )
    return sorted(windows)


def describe(opening_hours: dict[str, Any] | None, day: datetime) -> str:
    """Human-readable hours for one day, for use in explanations."""
    if parse_periods(opening_hours) is None:
        return "hours not published"
    windows = opens_on(opening_hours, day)
    if not windows:
        return "closed this day"
    return ", ".join(f"{start:%H:%M}-{end:%H:%M}" for start, end in windows)
