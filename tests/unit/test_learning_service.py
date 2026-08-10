"""The learning core: stored signals in, derived hypotheses out.

Everything here is pure - no database, no model, no clock beyond the
timestamps the signals carry - because the whole design is that a hypothesis
is a function of the evidence, recomputed on every read.
"""

from datetime import date, datetime, time, timezone

from app.config import Settings
from app.models.learning import LearningSignal, TripReflection, ReflectionItem
from app.models.rejection import RejectionRecord
from app.models.traveler import TravelerProfile
from app.models.trip import TripState, TripTraveler
from app.services.learning_service import (
    CATALOGUE,
    behavioral_signal_allowed,
    derive_hypotheses,
    profile_changes_for,
    remove_changes_for,
    signal_for_move,
    signal_for_replan,
    signals_for_reflection,
)
from tests.conftest import make_item, sample_state

SETTINGS = Settings(learning_min_signals=3, learning_min_trips=2)


def profile(**overrides) -> TravelerProfile:
    return TravelerProfile(profile_id="user_test", name="Haoyu", **overrides)


def signal(
    key: str = "avoid_early_mornings",
    trip: str = "trip_a",
    strength: str = "weak",
    source: str = "behavior_move",
    context: dict | None = None,
    at: datetime | None = None,
) -> LearningSignal:
    return LearningSignal(
        profile_id="user_test",
        trip_id=trip,
        preference_key=key,
        strength=strength,
        source=source,
        context=context or {"item": "Fish market", "from": "08:30", "to": "11:00",
                            "trip_title": "Tokyo"},
        observed_at=at or datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def solo_state() -> TripState:
    state = sample_state()
    state.travelers = [
        TripTraveler(traveler_id="trv_solo", profile_id="user_test", name="Haoyu",
                     role="organizer")
    ]
    return state


# --- attribution -------------------------------------------------------------


def test_behavioral_signals_require_exactly_one_traveler_with_a_profile():
    assert behavioral_signal_allowed(solo_state()) == "user_test"

    group = sample_state()  # two travelers
    assert behavioral_signal_allowed(group) is None

    anonymous = solo_state()
    anonymous.travelers[0].profile_id = None
    assert behavioral_signal_allowed(anonymous) is None


# --- signal builders ---------------------------------------------------------


def test_an_afternoon_move_or_a_small_nudge_is_not_a_signal():
    state = solo_state()
    afternoon = signal_for_move(
        state, item_title="Museum", old_start=time(14, 0), new_start=time(16, 0),
        profile_id="user_test",
    )
    nudge = signal_for_move(
        state, item_title="Market", old_start=time(8, 30), new_start=time(9, 0),
        profile_id="user_test",
    )
    real = signal_for_move(
        state, item_title="Market", old_start=time(8, 30), new_start=time(11, 0),
        profile_id="user_test",
    )
    assert afternoon is None
    assert nudge is None
    assert real is not None
    assert real.preference_key == "avoid_early_mornings"
    assert real.strength == "weak"
    assert real.context["from"] == "08:30" and real.context["to"] == "11:00"


def test_only_an_explicit_intensity_makes_a_replan_a_signal():
    state = solo_state()
    plain = signal_for_replan(state, intensity=None, when="2026-10-04", profile_id="user_test")
    relaxed = signal_for_replan(
        state, intensity="relaxed", when="2026-10-04", profile_id="user_test"
    )
    assert plain is None
    assert relaxed is not None and relaxed.preference_key == "relaxed_pace"


def test_reflection_emits_at_most_one_signal_per_key():
    state = solo_state()
    state.itinerary.days[0].items.append(
        make_item(
            "it_early",
            title="Temple at dawn",
            entity_id=None,
            start=datetime(2026, 10, 3, 8, 0),
            end=datetime(2026, 10, 3, 9, 0),
        )
    )
    reflection = TripReflection(
        days_too_busy=[date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)],
        skipped=[ReflectionItem(item_id="it_early", label="Temple at dawn")],
        answered_by="trv_solo",
    )
    signals = signals_for_reflection(state, reflection, "user_test")

    keys = [s.preference_key for s in signals]
    assert keys.count("relaxed_pace") == 1  # three busy days, one signal
    assert keys.count("avoid_early_mornings") == 1
    assert all(s.strength == "moderate" for s in signals)


def test_a_skipped_afternoon_item_says_nothing_about_mornings():
    state = solo_state()
    reflection = TripReflection(
        skipped=[ReflectionItem(item_id="it_1", label="Afternoon museum")],
        answered_by="trv_solo",
    )
    # it_1 in sample_state starts at 10:00 - not early.
    signals = signals_for_reflection(state, reflection, "user_test")
    assert signals == []


# --- derivation --------------------------------------------------------------


def test_one_signal_is_an_emerging_pattern_not_a_proposal():
    hyps = derive_hypotheses(profile(), [signal()], settings=SETTINGS)
    assert len(hyps) == 1
    assert hyps[0].status == "emerging"
    assert hyps[0].confidence == "emerging"


def test_breadth_requires_distinct_trips_not_just_repeats():
    same_trip = [signal(trip="trip_a"), signal(trip="trip_a"), signal(trip="trip_a")]
    hyps = derive_hypotheses(profile(), same_trip, settings=SETTINGS)
    assert hyps[0].confidence == "emerging"  # three signals, one trip

    two_trips = [signal(trip="trip_a"), signal(trip="trip_a"), signal(trip="trip_b")]
    hyps = derive_hypotheses(profile(), two_trips, settings=SETTINGS)
    assert hyps[0].confidence == "likely"
    assert hyps[0].status == "proposable"


def test_strength_is_the_strongest_expression_not_an_average():
    signals = [
        signal(trip="trip_a"),
        signal(trip="trip_a"),
        signal(trip="trip_b", strength="strong", source="stated",
               context={"quote": "我不是早起的人", "trip_title": "Kyoto"}),
    ]
    hyps = derive_hypotheses(profile(), signals, settings=SETTINGS)
    assert hyps[0].strength == "strong"
    assert hyps[0].confidence == "likely"  # strength and confidence move apart


def test_confidence_and_strength_move_independently():
    many_weak = [signal(trip=f"trip_{i}") for i in range(6)]
    for i, s in enumerate(many_weak):
        object.__setattr__(s, "trip_id", f"trip_{i % 3}")
    hyps = derive_hypotheses(profile(), many_weak, settings=SETTINGS)
    assert hyps[0].strength == "weak"
    assert hyps[0].confidence == "strong"  # 6 signals, 3 trips


def test_a_dismissed_hypothesis_never_becomes_proposable_however_much_evidence_arrives():
    base = derive_hypotheses(profile(), [signal()], settings=SETTINGS)[0]
    said_no = profile(
        learning_rejections=[
            RejectionRecord(
                target_kind="hypothesis", target_id=base.hypothesis_id, scope="profile"
            )
        ]
    )
    flood = [signal(trip=f"trip_{i}") for i in range(8)]
    hyps = derive_hypotheses(said_no, flood, settings=SETTINGS)
    assert hyps[0].hypothesis_id == base.hypothesis_id  # content hash held
    assert hyps[0].status == "dismissed"


def test_a_value_already_in_the_profile_marks_the_hypothesis_applied():
    signals = [signal(trip="trip_a"), signal(trip="trip_a"), signal(trip="trip_b")]
    hand_set = profile()
    hand_set.pace_preferences.preferred_start_time = "11:00"
    hyps = derive_hypotheses(hand_set, signals, settings=SETTINGS)
    assert hyps[0].status == "applied"  # the user got there first


def test_proposed_start_time_follows_observed_moves_and_is_clamped():
    to_1045 = [
        signal(context={"item": "a", "from": "08:30", "to": "10:45", "trip_title": "T"}),
    ]
    hyps = derive_hypotheses(profile(), to_1045, settings=SETTINGS)
    assert hyps[0].proposed_value == "10:30"  # rounded down to the half hour

    dramatic = [
        signal(context={"item": "a", "from": "08:30", "to": "13:00", "trip_title": "T"}),
    ]
    hyps = derive_hypotheses(profile(), dramatic, settings=SETTINGS)
    assert hyps[0].proposed_value == "11:30"  # clamped

    stated_only = [signal(source="stated", context={"quote": "not a morning person"})]
    hyps = derive_hypotheses(profile(), stated_only, settings=SETTINGS)
    assert hyps[0].proposed_value == "10:30"  # nothing observed, default


def test_every_catalogue_key_names_its_consumer():
    for key, entry in CATALOGUE.items():
        assert entry.consumer, f"{key} has no consumer - a preference that moves nothing"
        assert "." in entry.field_path


def test_evidence_lines_are_rendered_from_stored_context_only():
    hyps = derive_hypotheses(profile(), [signal()], settings=SETTINGS)
    line = hyps[0].evidence[0].line
    assert "Tokyo" in line and "08:30" in line and "11:00" in line


# --- profile change preparation ----------------------------------------------


def test_profile_changes_deep_merge_and_spare_sibling_fields():
    p = profile()
    p.pace_preferences.max_daily_walking_km = 6.0  # a hand-set sibling
    hyp = derive_hypotheses(
        p, [signal(trip="trip_a"), signal(trip="trip_a"), signal(trip="trip_b")],
        settings=SETTINGS,
    )[0]

    changes, provenance = profile_changes_for(p, hyp, hyp.proposed_value)

    assert changes["pace_preferences"]["preferred_start_time"] == hyp.proposed_value
    assert changes["pace_preferences"]["max_daily_walking_km"] == 6.0  # survived
    assert provenance.previous_value == "09:00"
    assert changes["learned"][hyp.field_path]["hypothesis_id"] == hyp.hypothesis_id


def test_remove_reverts_to_the_pre_learning_value():
    p = profile()
    p.pace_preferences.preferred_start_time = "09:30"  # hand-set before learning
    hyp = derive_hypotheses(
        p, [signal(trip="trip_a"), signal(trip="trip_a"), signal(trip="trip_b")],
        settings=SETTINGS,
    )[0]
    changes, _prov = profile_changes_for(p, hyp, "10:30")
    accepted = TravelerProfile.model_validate({**p.model_dump(), **changes})
    assert accepted.pace_preferences.preferred_start_time == "10:30"

    reverted = remove_changes_for(accepted, hyp.field_path)
    assert reverted["pace_preferences"]["preferred_start_time"] == "09:30"  # not "09:00"
    assert hyp.field_path not in reverted["learned"]


def test_a_malformed_learned_value_dies_in_validation_not_in_the_database():
    p = profile()
    hyp = derive_hypotheses(p, [signal()], settings=SETTINGS)[0]
    try:
        profile_changes_for(p, hyp, "25:99")
    except Exception:
        return
    raise AssertionError("a malformed ClockTime was accepted")
