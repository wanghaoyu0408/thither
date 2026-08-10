"""A learned start time finally moves the generated day.

`preferred_start_time` was stored, snapshotted, diffed and displayed - and
consumed by nothing (the ledger-10 defect class: a preference that influences
nothing). These pin its first consumer: the slot shift in generation and
replanning, and the parking weight beside it.
"""

from datetime import datetime, time

from app.models.group import TravelerPreferences
from app.models.traveler import PacePreferences
from app.models.trip import TripTraveler
from app.services.itinerary_service import (
    _TEMPLATES,
    _shifted,
    arrival_penalty,
    effective_start,
)
from tests.conftest import sample_state


def with_start(state, traveler_index: int, clock: str):
    prefs = TravelerPreferences(pace=PacePreferences(preferred_start_time=clock))
    state.travelers[traveler_index].preferences = prefs
    return state


# --- the shift itself --------------------------------------------------------


def test_default_preferences_leave_every_template_exactly_as_authored():
    for pace, slots in _TEMPLATES.items():
        assert _shifted(slots, time(9, 0)) == slots, pace
        assert _shifted(slots, None) == slots, pace


def test_a_late_riser_shifts_the_whole_day_not_just_the_first_slot():
    shifted = _shifted(_TEMPLATES["balanced"], time(10, 30))
    original = _TEMPLATES["balanced"]

    assert shifted[0].start == time(10, 30)
    # Every slot moved by the same 30 minutes; order and durations kept.
    for before, after in zip(original, shifted):
        gap = (after.start.hour * 60 + after.start.minute) - (
            before.start.hour * 60 + before.start.minute
        )
        assert gap == 30
        assert after.kind == before.kind and after.minutes == before.minutes


def test_dinner_never_slips_past_eight():
    # Balanced dinner is authored at 19:00, so the meal cap allows 60 minutes
    # of shift at most - even when the traveller asks for 11:30.
    shifted = _shifted(_TEMPLATES["balanced"], time(11, 30))
    assert shifted[0].start == time(11, 0)  # capped: 60, not 90
    dinner = [slot for slot in shifted if slot.kind == "meal"][-1]
    assert dinner.start == time(20, 0)


def test_the_most_morning_averse_traveler_sets_the_days_start():
    state = sample_state()
    with_start(state, 0, "09:30")
    with_start(state, 1, "10:30")
    assert effective_start(state) == time(10, 30)


def test_an_unresolved_traveler_contributes_the_neutral_default():
    state = sample_state()
    with_start(state, 0, "10:30")
    # traveler 1 has no snapshot -> neutral 09:00, which never wins a max.
    assert effective_start(state) == time(10, 30)


def test_generation_consumes_the_learned_start():
    """The acceptance-7 mechanism, at the unit level: snapshot in, later day out."""
    from app.services.itinerary_service import build_itinerary

    control = sample_state()
    late = sample_state()
    with_start(late, 0, "10:30")
    with_start(late, 1, "10:30")

    control_proposal = build_itinerary(control)
    late_proposal = build_itinerary(late)

    def first_start(proposal):
        return min(
            datetime.fromisoformat(item.start_at).time()
            for day in proposal.days
            for item in day.items
            if item.start_at
        )

    control_first = first_start(control_proposal)
    late_first = first_start(late_proposal)
    assert control_first < late_first
    assert late_first >= time(10, 30)


# --- the parking weight ------------------------------------------------------


def test_parking_penalty_doubles_only_for_a_parking_sensitive_group_that_is_driving():
    state = sample_state()
    state.brief.scope.rental_car = "plan"  # a trip with a car drives
    entity = next(iter(state.entities.values()))

    neutral = arrival_penalty(state, entity)
    assert neutral > 0.0

    sensitive = sample_state()
    sensitive.brief.scope.rental_car = "plan"
    sensitive.travelers[0].preferences = TravelerPreferences(
        pace=PacePreferences(parking_sensitive=True)
    )
    weighted = arrival_penalty(sensitive, entity)

    assert weighted == neutral * 2.0

    transit = sample_state()  # no rental car -> transit
    transit.travelers[0].preferences = TravelerPreferences(
        pace=PacePreferences(parking_sensitive=True)
    )
    assert arrival_penalty(transit, entity) == 0.0  # nobody parks on a train
