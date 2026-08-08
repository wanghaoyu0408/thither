"""Opening-hours parsing, against the traps real Tokyo data actually contains."""

from datetime import datetime

import pytest

from app.services.opening_hours import (
    OpenState,
    covers_visit,
    describe,
    opens_on,
    parse_periods,
    state_at,
    week_minute,
)

# Verbatim shape from the Places response M2 stored for shibuya zetton:
# a lunch/dinner split, and a dinner service closing at midnight the next day.
ZETTON = {
    "openNow": False,
    "periods": [
        {"open": {"day": 0, "hour": 16, "minute": 0}, "close": {"day": 0, "hour": 23, "minute": 0}},
        {
            "open": {"day": 1, "hour": 11, "minute": 30},
            "close": {"day": 1, "hour": 15, "minute": 0},
        },
        {"open": {"day": 1, "hour": 17, "minute": 0}, "close": {"day": 2, "hour": 0, "minute": 0}},
    ],
}

ALWAYS_OPEN = {"periods": [{"open": {"day": 0, "hour": 0, "minute": 0}}]}

# Saturday night into Sunday morning - wraps the end of the week.
LATE_BAR = {
    "periods": [
        {"open": {"day": 6, "hour": 22, "minute": 0}, "close": {"day": 0, "hour": 2, "minute": 0}}
    ]
}

# 2026-10-05 is a Monday, 2026-10-04 a Sunday, 2026-10-06 a Tuesday.
MONDAY = datetime(2026, 10, 5, 12, 0)
SUNDAY = datetime(2026, 10, 4, 18, 0)


def test_week_minute_uses_google_day_numbering():
    # Google counts Sunday as 0; Python counts Monday as 0.
    assert week_minute(datetime(2026, 10, 4, 0, 0)) == 0
    assert week_minute(datetime(2026, 10, 5, 0, 0)) == 24 * 60
    assert week_minute(datetime(2026, 10, 5, 11, 30)) == 24 * 60 + 690


# --- the openNow trap --------------------------------------------------------


def test_open_now_is_never_consulted():
    """It says False, and it is a snapshot from whenever this was fetched.

    Trusting it would mark the place shut for every future date.
    """
    assert ZETTON["openNow"] is False

    # Monday lunch service - genuinely open despite the stale flag.
    assert state_at(ZETTON, datetime(2026, 10, 5, 12, 0)) is OpenState.OPEN


def test_flipping_open_now_changes_nothing():
    lying = {**ZETTON, "openNow": True}

    assert state_at(lying, datetime(2026, 10, 5, 16, 0)) is OpenState.CLOSED


# --- unknown is not closed ---------------------------------------------------


@pytest.mark.parametrize("hours", [None, {}, {"openNow": True}, {"periods": []}])
def test_missing_hours_are_unknown_not_closed(hours):
    assert state_at(hours, MONDAY) is OpenState.UNKNOWN
    assert parse_periods(hours) is None


def test_unknown_survives_a_visit_window_check():
    assert covers_visit(None, MONDAY, datetime(2026, 10, 5, 14, 0)) is OpenState.UNKNOWN


# --- multiple periods per day ------------------------------------------------


def test_lunch_service_is_open():
    assert state_at(ZETTON, datetime(2026, 10, 5, 12, 0)) is OpenState.OPEN


def test_the_gap_between_lunch_and_dinner_is_closed():
    assert state_at(ZETTON, datetime(2026, 10, 5, 16, 0)) is OpenState.CLOSED


def test_dinner_service_is_open():
    assert state_at(ZETTON, datetime(2026, 10, 5, 19, 0)) is OpenState.OPEN


def test_a_day_with_no_period_is_closed():
    # Tuesday has no entry in this trimmed data.
    assert state_at(ZETTON, datetime(2026, 10, 6, 19, 0)) is OpenState.CLOSED


# --- boundaries --------------------------------------------------------------


def test_opening_minute_counts_as_open():
    assert state_at(ZETTON, datetime(2026, 10, 5, 11, 30)) is OpenState.OPEN


def test_closing_minute_counts_as_closed():
    """Half-open interval: arriving exactly at close is not arriving."""
    assert state_at(ZETTON, datetime(2026, 10, 5, 15, 0)) is OpenState.CLOSED


def test_one_minute_before_close_is_open():
    assert state_at(ZETTON, datetime(2026, 10, 5, 14, 59)) is OpenState.OPEN


# --- crossing midnight -------------------------------------------------------


def test_open_late_on_the_evening_it_opened():
    assert state_at(ZETTON, datetime(2026, 10, 5, 23, 30)) is OpenState.OPEN


def test_closed_after_the_midnight_close():
    assert state_at(ZETTON, datetime(2026, 10, 6, 0, 30)) is OpenState.CLOSED


def test_a_period_wrapping_the_end_of_the_week():
    # Saturday 23:00 - inside the Saturday-into-Sunday window.
    assert state_at(LATE_BAR, datetime(2026, 10, 10, 23, 0)) is OpenState.OPEN
    # Sunday 01:00 - still inside it, having wrapped past minute zero.
    assert state_at(LATE_BAR, datetime(2026, 10, 11, 1, 0)) is OpenState.OPEN
    # Sunday 03:00 - after the close.
    assert state_at(LATE_BAR, datetime(2026, 10, 11, 3, 0)) is OpenState.CLOSED


def test_wrapping_period_produces_two_intervals():
    assert len(parse_periods(LATE_BAR)) == 2


# --- 24 hours ----------------------------------------------------------------


def test_open_with_no_close_means_always_open():
    for moment in (
        datetime(2026, 10, 5, 3, 0),
        datetime(2026, 10, 8, 15, 0),
        datetime(2026, 10, 11, 23, 59),
    ):
        assert state_at(ALWAYS_OPEN, moment) is OpenState.OPEN


# --- whole-visit coverage ----------------------------------------------------


def test_a_visit_inside_one_period_is_open():
    assert (
        covers_visit(ZETTON, datetime(2026, 10, 5, 12, 0), datetime(2026, 10, 5, 13, 30))
        is OpenState.OPEN
    )


def test_a_visit_running_past_closing_is_not_open():
    """Arriving before close is not the same as being open for the whole meal."""
    assert (
        covers_visit(ZETTON, datetime(2026, 10, 5, 14, 30), datetime(2026, 10, 5, 16, 30))
        is OpenState.CLOSED
    )


def test_a_visit_spanning_the_afternoon_gap_is_not_open():
    assert (
        covers_visit(ZETTON, datetime(2026, 10, 5, 14, 0), datetime(2026, 10, 5, 18, 0))
        is OpenState.CLOSED
    )


def test_a_dinner_crossing_midnight_is_open():
    assert (
        covers_visit(ZETTON, datetime(2026, 10, 5, 22, 0), datetime(2026, 10, 5, 23, 45))
        is OpenState.OPEN
    )


def test_a_zero_length_visit_falls_back_to_a_point_check():
    moment = datetime(2026, 10, 5, 12, 0)

    assert covers_visit(ZETTON, moment, moment) is OpenState.OPEN


# --- explanation -------------------------------------------------------------


def test_windows_for_a_day_are_listed():
    windows = opens_on(ZETTON, MONDAY)

    assert len(windows) == 2
    assert windows[0][0].hour == 11 and windows[0][0].minute == 30
    assert windows[1][0].hour == 17


def test_sunday_has_a_single_window():
    assert len(opens_on(ZETTON, SUNDAY)) == 1


def test_description_is_human_readable():
    assert describe(ZETTON, MONDAY) == "11:30-15:00, 17:00-00:00"
    assert describe(None, MONDAY) == "hours not published"
    assert describe(ZETTON, datetime(2026, 10, 6, 12, 0)) == "closed this day"
