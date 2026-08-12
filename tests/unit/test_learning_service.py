"""The learning core: stored signals in, derived hypotheses out.

Everything here is pure - no database, no model, no clock beyond the
timestamps the signals carry - because the whole design is that a hypothesis
is a function of the evidence, recomputed on every read.
"""

from datetime import date, datetime, time, timedelta, timezone

from app.config import Settings
from app.models.common import Money
from app.models.decision import Decision, DecisionOption, FlightOptionData, HotelOptionData
from app.models.flight import FlightSegment, FlightSlice
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
    signals_for_choice,
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


# --- choosing between priced options -----------------------------------------
#
# The richest and most frequent thing a traveller does here, and for the whole
# of M9 the only one that taught nothing: a complete planning session - four
# cards chosen, an itinerary generated - left the Travel DNA panel empty,
# because no signal came from choosing.


def leg(stops: int, hour: int) -> FlightSlice:
    """A slice with real segments, because `FlightSlice.stops` counts them -
    a slice with none reports nonstop whatever the itinerary was."""
    depart = datetime(2026, 10, 3, hour, 0)
    return FlightSlice(
        origin="ALB",
        destination="ORD",
        departing_at=depart,
        arriving_at=depart + timedelta(hours=3),
        duration_minutes=180,
        segments=[
            FlightSegment(
                origin="ALB",
                destination="ORD",
                departing_at=depart + timedelta(hours=i),
                arriving_at=depart + timedelta(hours=i + 1),
                marketing_carrier="AA",
            )
            for i in range(stops + 1)
        ],
    )


def flight(price: float, stops: int | list[int], ref: str = "off") -> FlightOptionData:
    """`stops` as a list gives one entry per direction, the shape a return
    trip actually has and the one the scalar field cannot express."""
    per_leg = stops if isinstance(stops, list) else [stops]
    return FlightOptionData(
        provider="duffel",
        offer_ref=ref,
        live_mode=True,
        price=Money(amount=price),
        origin="ALB",
        destination="ORD",
        stops=per_leg[0],
        slices=[leg(n, 9 + 12 * i) for i, n in enumerate(per_leg)],
    )


def hotel(price: float, minutes: float | None, name: str = "Hotel") -> HotelOptionData:
    return HotelOptionData(
        provider="serpapi",
        live_mode=True,
        name=name,
        nightly_price=Money(amount=price),
        route_minutes={"ent_1": minutes} if minutes is not None else {},
    )


def chosen_from(*payloads, pick: int, name: str = "flights", rejected: set[int] = frozenset()):
    """Build the card, click one option, and report what it taught."""
    options = [
        DecisionOption(
            option_id=f"opt_{i}",
            data=payload,
            status="rejected" if i in rejected else "candidate",
        )
        for i, payload in enumerate(payloads)
    ]
    decision = Decision(options=options)
    return signals_for_choice(
        solo_state(),
        decision_name=name,
        decision=decision,
        chosen_option_id=f"opt_{pick}",
        profile_id="user_test",
    )


def test_paying_more_for_fewer_stops_says_nonstop_matters():
    signals = chosen_from(flight(312, 0), flight(248, 1), pick=0)

    assert [s.preference_key for s in signals] == ["values_nonstop"]
    assert signals[0].strength == "weak"  # a click is a click
    assert signals[0].source == "behavior_choice"
    assert "$312" in signals[0].context["chose"]
    assert "$248" in signals[0].context["over"]


def test_taking_the_cheap_connection_says_price_matters():
    signals = chosen_from(flight(312, 0), flight(248, 1), pick=1)

    assert [s.preference_key for s in signals] == ["flight_price_sensitive"]


def test_a_choice_that_gave_nothing_up_teaches_nothing():
    """The winner was both the cheapest and the nonstop. However firmly it was
    clicked, there is no priority in it to read - and reading one out anyway
    is how a profile fills up with preferences its owner never held."""
    assert chosen_from(flight(248, 0), flight(312, 1), pick=0) == []


def test_paying_more_for_more_stops_teaches_nothing():
    """They passed over cheaper money *and* took the longer routing, so they
    bought something this does not measure - the airline, the timing, a
    checked bag. Silence is the honest answer."""
    assert chosen_from(flight(312, 1), flight(248, 0), pick=0) == []


def test_the_return_leg_counts_toward_the_stops():
    """Nonstop out and a stop on the way home is not a nonstop trip. The
    scalar `stops` field describes the outbound only, so reading it alone
    would call this a nonstop and learn the opposite of what happened."""
    assert chosen_from(flight(312, [0, 1]), flight(248, [0, 0]), pick=0) == []

    signals = chosen_from(flight(312, [0, 0]), flight(248, [0, 1]), pick=0)
    assert [s.preference_key for s in signals] == ["values_nonstop"]


def test_a_rejected_option_is_not_a_road_not_taken():
    """It was refused earlier and is not on the card being chosen from, so it
    is not the thing that was given up."""
    assert chosen_from(flight(312, 0), flight(248, 1), pick=0, rejected={1}) == []


def test_the_only_option_on_a_card_teaches_nothing():
    assert chosen_from(flight(312, 0), pick=0) == []


def test_paying_more_to_stay_closer_says_location_matters():
    signals = chosen_from(
        hotel(180, 8, "Central"), hotel(120, 35, "Far"), pick=0, name="hotel"
    )

    assert [s.preference_key for s in signals] == ["hotel_location_matters"]
    assert "Central" in signals[0].context["chose"]
    assert "35 min" in signals[0].context["over"]


def test_taking_the_cheaper_room_further_out_says_price_matters():
    signals = chosen_from(
        hotel(180, 8, "Central"), hotel(120, 35, "Far"), pick=1, name="hotel"
    )

    assert [s.preference_key for s in signals] == ["hotel_price_sensitive"]


def test_an_unmeasured_option_is_skipped_rather_than_guessed():
    """No route minutes means no distance axis. Comparing against a hotel
    whose distance is unknown would be comparing against a made-up zero."""
    assert chosen_from(hotel(180, 8), hotel(120, None), pick=0, name="hotel") == []


def test_a_choice_with_no_price_on_it_is_not_a_tradeoff():
    """Airports and neighbourhoods carry drive minutes and no price at all, so
    a choice between them says nothing this can read. Inventing an axis to
    have something to learn would be worse than learning nothing."""
    from app.models.flight import AirportOption

    def airport(iata: str, minutes: float) -> AirportOption:
        return AirportOption(
            iata=iata,
            name=f"{iata} field",
            city="Chicago",
            lat=41.9,
            lng=-87.9,
            ground_travel_minutes=minutes,
        )

    signals = chosen_from(airport("ORD", 30), airport("MDW", 55), pick=0, name="arrival_airport")
    assert signals == []


def test_a_choice_signal_reads_back_without_its_trip():
    signals = chosen_from(flight(312, 0), flight(248, 1), pick=0)
    hyps = derive_hypotheses(profile(), signals, settings=SETTINGS)

    line = hyps[0].evidence[0].line
    assert "chose" in line and "$312" in line and "$248" in line
    assert "nonstop" in line


def test_a_return_trip_says_its_stop_count_is_a_total():
    """The card reports each direction on its own line. "2 stops" beside
    "Outbound 1 stop / Return 1 stop" reads as a third figure, not their sum."""
    signals = chosen_from(flight(312, [0, 0]), flight(248, [1, 1]), pick=0)

    assert signals[0].context["chose"].endswith("nonstop both ways")
    assert signals[0].context["over"].endswith("2 stops in all")

    # One-way keeps the plain wording - there is no total to distinguish.
    one_way = chosen_from(flight(312, [0]), flight(248, [1]), pick=0)
    assert one_way[0].context["chose"].endswith("nonstop")
    assert one_way[0].context["over"].endswith("1 stop")


def test_an_accepted_choice_preference_lands_on_the_ranker_s_own_weight():
    """The point of the exercise: an accepted hypothesis has to change the
    number `flight_ranking` multiplies by, or the whole loop is theatre."""
    signals = [
        s
        for trip in ("trip_a", "trip_a", "trip_b")
        for s in chosen_from(flight(312, 0), flight(248, 1), pick=0)
        for s in [s.model_copy(update={"trip_id": trip})]
    ]
    hyp = derive_hypotheses(profile(), signals, settings=SETTINGS)[0]
    assert hyp.status == "proposable"

    changes, provenance = profile_changes_for(profile(), hyp, hyp.proposed_value)

    assert changes["flight_preferences"]["nonstop_importance"] == hyp.proposed_value
    assert changes["flight_preferences"]["nonstop_importance"] > 0.5  # the default
    # Siblings survive the write, and the reason is stored beside the value.
    assert changes["flight_preferences"]["price_importance"] == 0.5
    assert provenance.previous_value == 0.5


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


def test_every_learnable_key_is_in_the_catalogue_and_can_actually_be_written():
    """Two ways to mint a key that can never reach a profile, both silent.

    A `PreferenceKey` with no catalogue entry is dropped by `derive_hypotheses`
    without a word; a catalogue entry whose section is missing from
    `_SECTION_FOR_PATH` derives and proposes happily and then raises at the
    moment the traveller presses Add. Both were one line away while the
    choice keys were being added.
    """
    from typing import get_args

    from app.models.learning import PreferenceKey
    from app.services.learning_service import _SECTION_FOR_PATH

    assert set(get_args(PreferenceKey)) == set(CATALOGUE)
    for key, entry in CATALOGUE.items():
        section, field = entry.field_path.split(".", 1)
        assert section in _SECTION_FOR_PATH, f"{key} proposes into an unwritable section"
        assert field in type(getattr(profile(), section)).model_fields, (
            f"{key} names {entry.field_path}, which is not a field"
        )


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
