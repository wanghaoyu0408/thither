"""The stress-test engine: figures in, windows and findings out. Pure.

Everything here is arithmetic a person could redo on paper, which is the
point - the model explains this output and computes none of it.
"""

from datetime import UTC, date, datetime, time, timedelta

from app.config import Settings
from app.models.arrival import ArrivalContext, ParkingContext, ParkingSpot
from app.models.calibration import Outcome, Prediction
from app.models.simulation import SCENARIOS
from app.models.validation import ValidationIssue
from app.models.weather import WeatherContext
from app.services.calibration_service import Calibrations, outcome_for
from app.services.simulation_service import (
    ASSUMPTIONS,
    MEAL_GAP_HOURS,
    resolve_assumptions,
    simulate_trip,
)
from app.services.validation_service import TravelLookup
from tests.conftest import make_entity, make_item, sample_state

DAY = date(2026, 10, 3)
SETTINGS = Settings(calibration_min_samples=5, calibration_confident_samples=12)


def at(hh: int, mm: int = 0) -> datetime:
    return datetime.combine(DAY, time(hh, mm))


def trip(*items, driving: bool = False, entities: dict | None = None):
    """A one-day trip with exactly the given items, walking unless driving."""
    state = sample_state()
    state.brief.timezone = "Asia/Tokyo"
    if driving:
        state.brief.scope.rental_car = "plan"
    day = state.itinerary.days[0]
    day.items = list(items)
    for entity_id, categories in (entities or {}).items():
        if entity_id not in state.entities:
            state.entities[entity_id] = make_entity(entity_id, entity_id)
        state.entities[entity_id].categories = list(categories)
        # Far apart, so mode_between picks the long-haul mode when driving.
        state.entities[entity_id].lat = 35.6 + 0.2 * len(state.entities)
        state.entities[entity_id].lng = 139.7
    return state


def stop(title, start, end, entity="ent_cafe", **fields):
    """A plain flexible activity unless told otherwise.

    `make_item`'s defaults are a fixed-time restaurant, which is right for the
    fixtures it was built for and would make every stop here a commitment and
    a meal - the two things these tests exist to vary.
    """
    item = make_item(
        f"item_{title.lower().replace(' ', '_')}",
        title=title,
        entity_id=entity,
        start=start,
        end=end,
        cost=None,
    )
    return item.model_copy(
        update={"type": "activity", "time_flexibility": "flexible", **fields}
    )


def measured(*legs, mode: str = "walking") -> TravelLookup:
    lookup = TravelLookup()
    for origin, destination, minutes in legs:
        lookup.minutes[(origin, destination, mode)] = minutes
        lookup.meters[(origin, destination, mode)] = minutes * 80.0
    return lookup


def forecast_for(state, **kwargs):
    kwargs.setdefault("issues", [])
    kwargs.setdefault("settings", SETTINGS)
    return simulate_trip(state, **kwargs)


def only_day(forecast):
    assert len(forecast.days) == 1
    return forecast.days[0]


# --- the arithmetic -----------------------------------------------------------


def test_a_measured_walk_brackets_the_claim_not_replaces_it():
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
    )
    day = only_day(forecast_for(state, travel=measured(("ent_museum", "ent_cafe", 20.0))))

    dinner = day.stops[1]
    expected = dinner.arrival["expected"]
    conservative = dinner.arrival["conservative"]
    optimistic = dinner.arrival["optimistic"]

    # Expected is the figure as claimed: museum ends 16:00 + 20 min.
    assert expected.low == expected.high == at(16, 20)
    # Conservative reaches up by the walking spread, never below the claim.
    assert conservative.low == at(16, 20)
    assert conservative.high == at(16, 23)  # 20 * 1.15
    # Optimistic reaches down, never above the claim.
    assert optimistic.high == at(16, 20)
    assert optimistic.low < at(16, 20)


def test_the_spec_example_a_tight_reservation_is_fragile_only_in_conservative():
    """The 11:00 reservation from the spec, built as a fixture: expected makes
    it, conservative may not, and that exact asymmetry is the verdict."""
    state = trip(
        stop("Coffee", at(9), at(10, 30), "ent_museum"),
        stop(
            "Lunch reservation",
            at(11),
            at(12),
            "ent_cafe",
            time_flexibility="fixed",
            reservation_booked=True,
        ),
    )
    # 20 minutes measured: expected arrival 10:50 (safe by 10), conservative
    # 10:50-10:53... still safe. Use 25: expected 10:55 (safe with 5min spare),
    # conservative up to 10:58.75 -> inside the buffer -> tight. Use 28:
    # expected 10:58 (buffer eaten but on time), conservative 10:58-11:02 ->
    # late_arrival_risk.
    day = only_day(forecast_for(state, travel=measured(("ent_museum", "ent_cafe", 28.0))))

    lunch = day.stops[1]
    assert lunch.committed
    assert lunch.arrival["expected"].high <= at(11)
    assert lunch.arrival["conservative"].high > at(11)
    kinds = [finding.kind for finding in day.findings]
    assert "late_arrival_risk" in kinds
    assert day.verdict == "fragile"


def test_a_roomy_day_of_measured_legs_is_comfortable():
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe", type="restaurant"),
    )
    day = only_day(forecast_for(state, travel=measured(("ent_museum", "ent_cafe", 15.0))))

    assert day.verdict == "comfortable"
    assert (day.legs_measured, day.legs_total) == (1, 1)
    assert day.findings == []


def test_lateness_cascades_through_flexible_items_and_is_eaten_by_fixed_ones():
    state = trip(
        stop("Museum", at(14), at(15), "ent_museum"),
        # Flexible lunch starts when you get there and keeps its hour.
        stop("Late lunch", at(15), at(16), "ent_cafe"),
        # The show starts and ends when it says, however late you are.
        stop(
            "Show",
            at(16, 30),
            at(18),
            "ent_museum",
            time_flexibility="fixed",
        ),
    )
    travel = measured(("ent_museum", "ent_cafe", 30.0), ("ent_cafe", "ent_museum", 30.0))
    day = only_day(forecast_for(state, travel=travel))

    lunch, show = day.stops[1], day.stops[2]
    # Arrived 15:30 expected; the hour of lunch shifts wholesale.
    assert lunch.departure["expected"].low == at(16, 30)
    # The show's departure is its written end in every scenario - lateness
    # eats the visit, it does not push the evening.
    for scenario in SCENARIOS:
        assert show.departure[scenario].low == at(18)
        assert show.departure[scenario].high == at(18)


# --- unknowns are never zero --------------------------------------------------


def test_an_unmeasured_leg_advances_nothing_and_flags_everything_after_it():
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe", reservation_booked=True),
    )
    day = only_day(forecast_for(state, travel=TravelLookup()))

    dinner = day.stops[1]
    assert dinner.rests_on_unknown
    # The window did not silently become "16:00 + 0 min": it reset to the
    # schedule and said so.
    assert [e.provenance for e in dinner.inputs] == ["unknown"]
    kinds = [finding.kind for finding in day.findings]
    assert "unknown_dependency" in kinds
    # And no lateness claim was derived from an assumed schedule.
    assert "late_arrival_risk" not in kinds
    assert "tight_buffer" not in kinds
    assert day.verdict == "workable", "unknown caps the day, in both directions"


def test_a_placeless_transition_is_not_an_unknown_journey():
    """Free time carries no claim of a journey, so it neither invents minutes
    nor poisons the chain."""
    state = trip(
        stop("Museum", at(14), at(15), "ent_museum"),
        stop("Rest at the hotel", at(15), at(16), None, type="free_time"),
        stop("Dinner", at(17), at(18), "ent_cafe"),
    )
    day = only_day(forecast_for(state, travel=TravelLookup()))

    assert not day.stops[1].rests_on_unknown
    assert day.legs_total == 0  # no entity-to-entity leg exists to count


# --- parking ------------------------------------------------------------------


def driving_trip(arrival_ctx: ArrivalContext | None = None):
    state = trip(
        stop("Trailhead", at(9), at(10), "ent_far_a"),
        stop("Lookout", at(11), at(12), "ent_far_b"),
        driving=True,
        entities={"ent_far_a": ["park"], "ent_far_b": ["park"]},
    )
    if arrival_ctx is not None:
        state.arrival[arrival_ctx.entity_id] = arrival_ctx
    return state


def test_unknown_parking_is_uncertainty_not_no_parking():
    state = driving_trip()
    day = only_day(
        forecast_for(state, travel=measured(("ent_far_a", "ent_far_b", 25.0), mode="driving"))
    )

    lookout = day.stops[1]
    parking = [e for e in lookout.inputs if e.what == "parking"]
    assert [e.provenance for e in parking] == ["assumption"]
    assert (parking[0].low, parking[0].high) == (5.0, 15.0)
    finding = next(f for f in day.findings if f.kind == "parking_uncertainty")
    assert "unverified" in finding.message
    assert not finding.breaks, "uncertainty is not unavailability"
    # The conservative arrival carries the assumption's high end.
    assert lookout.arrival["conservative"].high >= at(10) + timedelta(minutes=25 * 1.25 + 15)


def test_a_measured_parking_walk_replaces_the_assumption():
    context = ArrivalContext(
        entity_id="ent_far_b",
        mode="driving",
        parking=ParkingContext(
            availability="likely",
            spots=[
                ParkingSpot(
                    kind="parking_lot", cost="paid", name="North lot", walking_minutes=7.0
                )
            ],
        ),
    )
    day = only_day(
        forecast_for(
            driving_trip(context),
            travel=measured(("ent_far_a", "ent_far_b", 25.0), mode="driving"),
        )
    )

    parking = [e for e in day.stops[1].inputs if e.what == "parking"]
    assert [e.provenance for e in parking] == ["measured"]
    assert parking[0].low == parking[0].high == 7.0
    assert all(f.kind != "parking_uncertainty" for f in day.findings)


def test_a_walking_trip_involves_no_parking_at_all():
    state = trip(
        stop("Museum", at(14), at(15), "ent_museum"),
        stop("Cafe", at(16), at(17), "ent_cafe"),
    )
    day = only_day(forecast_for(state, travel=measured(("ent_museum", "ent_cafe", 12.0))))

    assert all(e.what != "parking" for stop_ in day.stops for e in stop_.inputs)


# --- calibration --------------------------------------------------------------


def calibrated_corpus(n: int = 12) -> Calibrations:
    outcomes = [
        outcome_for(
            Prediction(
                trip_id="trip_x",
                provider="google_routes",
                dimension="travel_minutes",
                mode="walking",
                scope="Asia/Tokyo",
                value=20.0,
                subject=f"s{i}",
            ),
            answer="a_bit_longer",
        )
        for i in range(n)
    ]
    return Calibrations.of(outcomes, scope="Asia/Tokyo", settings=SETTINGS)


def test_an_earned_band_replaces_the_assumption_spread():
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
    )
    travel = measured(("ent_museum", "ent_cafe", 20.0))

    plain = only_day(forecast_for(state, travel=travel)).stops[1]
    with_record = only_day(
        forecast_for(state, travel=travel, calibrations=calibrated_corpus(12))
    ).stops[1]

    assert [e.provenance for e in plain.inputs if e.what == "route"] == ["measured"]
    assert [e.provenance for e in with_record.inputs if e.what == "route"] == ["calibrated"]
    # The record says walks run 15-50% long; conservative widens accordingly.
    assert (
        with_record.arrival["conservative"].high > plain.arrival["conservative"].high
    )


def test_a_provisional_record_moves_nothing():
    """M10's rule holds here too: provisional evidence may be shown elsewhere,
    it never changes an output."""
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
    )
    travel = measured(("ent_museum", "ent_cafe", 20.0))

    plain = only_day(forecast_for(state, travel=travel)).stops[1]
    provisional = only_day(
        forecast_for(state, travel=travel, calibrations=calibrated_corpus(6))
    ).stops[1]

    assert provisional.arrival == plain.arrival
    assert [e.provenance for e in provisional.inputs if e.what == "route"] == ["measured"]


# --- weather, sunset, meals ---------------------------------------------------


def rainy_forecast(chance: float = 0.8) -> WeatherContext:
    return WeatherContext(
        date=DAY,
        kind="forecast",
        precipitation_probability=chance,
        source="google_weather",
        sunset=datetime(2026, 10, 3, 8, 30, tzinfo=UTC),  # 17:30 in Tokyo
    )


def test_forecast_rain_folds_in_and_makes_the_outdoor_day_fragile():
    """The validator already judges weather; the simulation folds its word in
    rather than recomputing the threshold."""
    state = trip(
        stop("Garden", at(14), at(16), "ent_far_a"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
        entities={"ent_far_a": ["garden"]},
    )
    state.itinerary.days[0].weather = rainy_forecast()
    day = only_day(
        forecast_for(
            state,
            travel=measured(("ent_far_a", "ent_cafe", 15.0)),
            issues=None,  # let it consult the real validator
        )
    )

    weather = next(f for f in day.findings if f.kind == "weather_exposure")
    assert weather.breaks
    assert "chance of rain" in weather.message  # the validator's own sentence
    assert any("validator" in line for line in weather.evidence)
    assert day.verdict == "fragile"


def test_a_seasonal_norm_informs_and_never_makes_a_day_fragile():
    state = trip(
        stop("Garden", at(14), at(16), "ent_far_a"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
        entities={"ent_far_a": ["garden"]},
    )
    state.itinerary.days[0].weather = WeatherContext(
        date=DAY, kind="historical_norm", precipitation_day_frequency=0.7
    )
    day = only_day(
        forecast_for(state, travel=measured(("ent_far_a", "ent_cafe", 15.0)), issues=None)
    )

    weather = next(f for f in day.findings if f.kind == "weather_exposure")
    assert not weather.breaks
    assert "seasonal" in weather.message
    assert day.verdict == "workable"


def test_an_outdoor_stop_running_past_sunset_is_fragile():
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Park walk", at(16, 30), at(18), "ent_far_a"),
        entities={"ent_far_a": ["park"]},
    )
    state.itinerary.days[0].weather = rainy_forecast(chance=0.0)
    # The park sits far out, so mode_between picks transit for the leg.
    day = only_day(
        forecast_for(state, travel=measured(("ent_museum", "ent_far_a", 10.0), mode="transit"))
    )

    sunset = next(f for f in day.findings if f.kind == "sunset_risk")
    assert sunset.breaks
    assert "17:30" in sunset.message
    assert day.verdict == "fragile"


def test_an_indoor_stop_after_sunset_is_nobodys_business():
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
    )
    state.itinerary.days[0].weather = rainy_forecast(chance=0.0)
    day = only_day(forecast_for(state, travel=measured(("ent_museum", "ent_cafe", 12.0))))

    assert all(f.kind != "sunset_risk" for f in day.findings)


def test_a_long_stretch_with_no_meal_is_named():
    state = trip(
        stop("Museum", at(10), at(12), "ent_museum"),
        stop("Gallery", at(12, 30), at(19, 30), "ent_cafe"),
    )
    day = only_day(
        forecast_for(state, travel=measured(("ent_museum", "ent_cafe", 10.0)))
    )

    gap = next(f for f in day.findings if f.kind == "meal_gap")
    assert "nothing to eat" in gap.message
    assert not gap.breaks
    assert day.verdict == "workable"


def test_an_ordinary_lunch_and_dinner_day_has_no_meal_finding():
    """The stock templates end lunch ~13:30 and start dinner at 19:00. A
    threshold that flagged that would teach people to ignore the finding."""
    state = trip(
        stop("Lunch", at(12, 30), at(13, 30), "ent_cafe", type="restaurant"),
        stop("Museum", at(14), at(18), "ent_museum"),
        stop("Dinner", at(19), at(20), "ent_cafe", type="restaurant"),
    )
    travel = measured(("ent_cafe", "ent_museum", 10.0), ("ent_museum", "ent_cafe", 10.0))
    day = only_day(forecast_for(state, travel=travel))

    assert all(f.kind != "meal_gap" for f in day.findings)
    assert MEAL_GAP_HOURS == 6.0


# --- the validator decides blocking -------------------------------------------


def test_a_validator_error_makes_the_day_blocking():
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
    )
    error = ValidationIssue(
        severity="error",
        type="travel_time_infeasible",
        item_ids=["item_dinner"],
        message="cannot be done as written",
    )
    day = only_day(
        forecast_for(state, travel=measured(("ent_museum", "ent_cafe", 12.0)), issues=[error])
    )

    assert day.verdict == "blocking"
    # The error is the verdict, not a re-dressed finding.
    assert all("cannot be done" not in f.message for f in day.findings)


def test_folded_warnings_carry_the_validators_words_not_new_numbers():
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
    )
    warning = ValidationIssue(
        severity="warning",
        type="day_overloaded",
        item_ids=[],
        message="7 items against a balanced-pace limit of 7",
    )
    day = only_day(
        forecast_for(state, travel=measured(("ent_museum", "ent_cafe", 12.0)), issues=[warning])
    )

    pace = next(f for f in day.findings if f.kind == "pace_mismatch")
    assert pace.message == warning.message  # folded, not recomputed


# --- the frame ----------------------------------------------------------------


def test_the_forecast_writes_nothing():
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe", reservation_booked=True),
    )
    state.itinerary.days[0].weather = rainy_forecast()
    snapshot = state.model_dump(mode="json")

    forecast_for(state, travel=measured(("ent_museum", "ent_cafe", 45.0)), issues=None)

    assert state.model_dump(mode="json") == snapshot


def test_the_verdict_vocabulary_is_four_words_and_no_numbers():
    from typing import get_args

    from app.models.simulation import Verdict

    assert set(get_args(Verdict)) == {"comfortable", "workable", "fragile", "blocking"}


def test_every_assumption_names_its_override_or_admits_none_exists():
    """The Travel Twin hook: each entry either points at a resolved-preference
    field that exists today, or says None - never at a capability nobody
    built (the ledger-44 mistake)."""
    from app.models.group import TravelerPreferences

    prefs = TravelerPreferences()
    for name, entry in ASSUMPTIONS.items():
        assert entry.label, f"{name} has no screen wording"
        if entry.overridable_by is None:
            continue
        section, field = entry.overridable_by.split(".", 1)
        sub = getattr(prefs, section)
        assert hasattr(sub, field), f"{name} names {entry.overridable_by}, which does not exist"


def test_resolve_assumptions_is_a_seam_and_currently_a_passthrough():
    assert resolve_assumptions(sample_state()) is ASSUMPTIONS


def test_a_single_day_can_be_previewed_alone():
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
    )
    forecast = simulate_trip(
        state, travel=measured(("ent_museum", "ent_cafe", 12.0)), issues=[], target_date=DAY
    )

    assert [d.date for d in forecast.days] == [DAY]
