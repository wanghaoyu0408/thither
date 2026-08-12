"""The calibration core: claims out of a trip, outcomes in, a measured bias out.

Pure, like `test_learning_service.py` and for the same reason - a calibration
is a function of the corpus, recomputed on every read, and the only stored
thing is what happened.
"""


import pytest

from app.config import Settings
from app.models.calibration import DIMENSIONS, Outcome, Prediction
from app.models.common import Money
from app.models.decision import Decision, DecisionOption, HotelAreaOption, HotelOptionData
from app.models.flight import AirportOption
from app.models.hotel import HotelPriceQuote
from app.models.weather import ClimatologyMethod, WeatherContext
from app.services.calibration_service import (
    ANSWER_BANDS,
    automatic_outcomes,
    calibrate,
    calibration_for,
    dimensions_never_checked,
    evidence_line,
    outcome_for,
    outcome_from_measurement,
    predictions_from,
    question_text,
    questions_for,
    scope_of,
)
from tests.conftest import sample_state

SETTINGS = Settings(calibration_min_samples=5, calibration_confident_samples=12)


def prediction(
    value: float = 14.0,
    *,
    mode: str | None = "transit",
    scope: str = "Asia/Tokyo",
    subject: str = "hotel_area:Ginza",
    dimension: str = "travel_minutes",
) -> Prediction:
    return Prediction(
        trip_id="trip_test",
        provider="google_routes",
        dimension=dimension,
        mode=mode,
        scope=scope,
        value=value,
        subject=subject,
    )


def answered(
    n: int, answer: str = "a_bit_longer", *, value: float = 14.0, **fields
) -> list[Outcome]:
    return [
        outcome_for(prediction(value, subject=f"s{i}", **fields), answer=answer)
        for i in range(n)
    ]


def area(name: str, minutes: float | None, mode: str = "transit") -> HotelAreaOption:
    return HotelAreaOption(area_name=name, mean_minutes=minutes, travel_mode=mode)


def airport(iata: str, minutes: float | None, source: str = "routes_api") -> AirportOption:
    return AirportOption(
        iata=iata,
        name=f"{iata} field",
        city="Tokyo",
        lat=35.6,
        lng=139.7,
        ground_travel_minutes=minutes,
        ground_travel_source=source,
    )


def trip_with(decision_name: str, *options, selected: int | None = None):
    state = sample_state()
    state.brief.timezone = "Asia/Tokyo"
    built = [
        DecisionOption(option_id=f"opt_{i}", data=payload) for i, payload in enumerate(options)
    ]
    setattr(
        state.decisions,
        decision_name,
        Decision(
            options=built,
            selected_option_id=None if selected is None else f"opt_{selected}",
        ),
    )
    return state


# --- predictions are derived, never stored -----------------------------------


def test_a_figure_already_in_the_trip_needs_no_new_write_path():
    state = trip_with("hotel_area", area("Ginza", 14.0), area("Asakusa", 27.0), selected=0)

    predictions = predictions_from(state)

    assert [p.value for p in predictions] == [14.0, 27.0]
    assert [p.drove_the_choice for p in predictions] == [True, False]
    assert {p.mode for p in predictions} == {"transit"}
    assert {p.scope for p in predictions} == {"Asia/Tokyo"}


def test_the_same_trip_derives_the_same_prediction_ids_every_time():
    """Identity is a content hash, which is what lets an outcome recorded today
    find its prediction again tomorrow without either being written down."""
    state = trip_with("hotel_area", area("Ginza", 14.0))

    first = predictions_from(state)[0].prediction_id
    # Re-measured a little differently: still the same claim about the same
    # thing, so still the same id.
    state.decisions.hotel_area.options[0].data.mean_minutes = 15.5
    assert predictions_from(state)[0].prediction_id == first


def test_a_drive_time_that_was_never_measured_is_not_a_claim():
    """`ground_travel_source` says whether anyone looked. An absent measurement
    cannot be a wrong one - invariant 1, applied to our own figures."""
    state = trip_with(
        "departure_airport",
        airport("HND", 30.0, "routes_api"),
        airport("NRT", 70.0, "not_looked_up"),
        airport("IBR", None, "unavailable"),
    )

    assert [p.subject for p in predictions_from(state)] == ["airport:HND"]


def test_a_historical_norm_is_never_checked_against_one_day():
    """A norm is a claim about a season. Scoring it against a single Tuesday is
    the category error `app/models/weather.py` exists to prevent."""
    state = sample_state()
    state.brief.timezone = "Asia/Tokyo"
    when = state.itinerary.days[0].date
    state.itinerary.days[0].weather = WeatherContext(
        date=when,
        kind="historical_norm",
        high_c=29.0,
        norm=ClimatologyMethod(
            provider="open-meteo",
            dataset="ERA5",
            sample_year_start=2005,
            sample_year_end=2025,
            calendar_window_days=7,
            sample_count=140,
        ),
    )

    assert predictions_from(state) == []

    state.itinerary.days[0].weather = WeatherContext(
        date=when, kind="forecast", high_c=29.0, source="google_weather"
    )
    forecast = predictions_from(state)
    assert [p.dimension for p in forecast] == ["day_high_c"]
    assert forecast[0].applies_to == when


def test_a_rejected_option_is_not_a_claim_anyone_is_standing_behind():
    state = trip_with("hotel_area", area("Ginza", 14.0), area("Asakusa", 27.0))
    state.decisions.hotel_area.options[1].status = "rejected"

    assert [p.subject for p in predictions_from(state)] == ["hotel_area:Ginza"]


def test_the_scope_key_refuses_free_text_country():
    """This database holds both "Japan" and "日本" for one country. Two buckets
    that split the evidence and then disagree is worse than one honest
    "unknown", which the backoff chain already handles."""
    state = sample_state()
    state.brief.timezone = None
    state.brief.destination.country = "日本"

    assert scope_of(state) == "unknown"

    state.brief.timezone = "Asia/Tokyo"
    assert scope_of(state) == "Asia/Tokyo"


# --- outcomes are intervals --------------------------------------------------


def test_every_answer_becomes_the_same_shape():
    for answer in ANSWER_BANDS:
        outcome = outcome_for(prediction(20.0), answer=answer)
        assert outcome is not None
        assert outcome.actual_low < outcome.actual_high
        assert outcome.predicted == 20.0


def test_an_exact_figure_beats_the_chip_and_has_no_width():
    """Somebody who timed the walk knows better than the band they also ticked."""
    outcome = outcome_for(prediction(14.0), answer="about_right", exact=23.0)

    assert (outcome.actual_low, outcome.actual_high) == (23.0, 23.0)
    assert outcome.relative_error == pytest.approx((0.642857, 0.642857), abs=1e-5)


def test_an_answer_that_means_nothing_records_nothing():
    assert outcome_for(prediction(14.0), answer="dunno") is None


def test_an_outcome_carries_no_trip_and_no_place():
    """It outlives the trip, so it must not become a travel history on the way."""
    stored = outcome_for(prediction(14.0), answer="about_right").model_dump()

    assert "trip_id" not in stored
    assert not any(key in stored for key in ("lat", "lng", "subject", "subject_label"))
    assert stored["scope"] == "Asia/Tokyo"  # region-coarse, and that is all


def hotel(name: str, headline: float | None, quoted: float | None) -> HotelOptionData:
    return HotelOptionData(
        provider="serpapi_google_hotels",
        live_mode=True,
        name=name,
        entity_id=f"ent_{name}",
        headline_nightly=None if headline is None else Money(amount=headline),
        quotes=(
            []
            if quoted is None
            else [HotelPriceQuote(source="a booking site", nightly=Money(amount=quoted))]
        ),
    )


def test_an_advertised_rate_checks_itself_the_moment_it_is_made():
    """Both numbers arrive in the same fetch, so this one needs no person, no
    network and no waiting. Ledger 4 found it by hand once - an advertised $70
    that no listed site would honour - and nothing counted it after that."""
    state = trip_with("hotel", hotel("Cheap Inn", 70.0, 90.0), selected=0)

    outcomes = automatic_outcomes(state)

    assert len(outcomes) == 1
    assert outcomes[0].dimension == "hotel_headline_gap"
    assert (outcomes[0].predicted, outcomes[0].actual_low) == (70.0, 90.0)
    assert outcomes[0].checked_by == "same_fetch"
    assert outcomes[0].relative_error[0] == pytest.approx(0.2857, abs=1e-3)


def test_a_hotel_with_nothing_to_compare_checks_nothing():
    state = trip_with(
        "hotel",
        hotel("No headline", None, 90.0),
        hotel("No quote anyone can be named for", 70.0, None),
    )

    assert automatic_outcomes(state) == []


def test_the_same_advertised_rate_records_once_however_often_it_is_derived():
    """The runner calls this at the end of every turn. Without a stable id the
    same claim would be counted again each time and a provider would look
    consistently wrong by repetition."""
    state = trip_with("hotel", hotel("Cheap Inn", 70.0, 90.0))

    first = automatic_outcomes(state)[0]
    second = automatic_outcomes(state)[0]

    assert first.prediction_id == second.prediction_id


# --- calibration -------------------------------------------------------------


def test_below_the_minimum_it_says_nothing_at_all():
    calibration = calibration_for(
        answered(3),
        provider="google_routes",
        dimension="travel_minutes",
        mode="transit",
        scope="Asia/Tokyo",
        settings=SETTINGS,
    )

    assert calibration.status == "uncalibrated"
    assert calibration.bias is None
    assert calibration.low_error is None and calibration.high_error is None
    # And it still reports how little it has, so a surface can say "checked
    # three times, which is not enough" rather than fall silent.
    assert calibration.sample_count == 3
    assert calibrate(14.0, calibration).adjusted is False


def test_one_road_closure_does_not_become_a_finding():
    """The median is the whole reason this is safe on a small corpus. A single
    95-minute journey against a 14-minute estimate is a +579% error; the mean
    would report it as the provider's character."""
    ordinary = answered(5)
    with_outlier = ordinary + [
        outcome_for(prediction(14.0, subject="crash"), answer="", exact=95.0)
    ]

    def bias(corpus):
        return calibration_for(
            corpus,
            provider="google_routes",
            dimension="travel_minutes",
            mode="transit",
            scope="Asia/Tokyo",
            settings=SETTINGS,
        ).bias

    assert bias(with_outlier) == pytest.approx(bias(ordinary), abs=0.01)


def test_a_mostly_right_provider_still_reports_its_bad_days():
    """The band is quantiles of the observed errors, not a spread computed
    around the median, and this is why.

    Thirty advertised hotel rates from the live database: fifteen matched
    exactly and three were understated by 13%, 20% and 67%. Median absolute
    deviation collapsed to ±1.6% on the strength of the fifteen, and an
    advertised $200 was reported as "more likely $197-$203" - a tight interval
    wrapped around a fat tail, which is a more confident lie than no interval.
    """
    exact = [
        outcome_from_measurement(prediction(100.0, mode=None, subject=f"m{i}"), 100.0)
        for i in range(15)
    ]
    bad = [
        outcome_from_measurement(prediction(100.0, mode=None, subject=f"b{i}"), actual)
        for i, actual in enumerate((113.0, 120.0, 167.0))
    ]

    calibration = calibration_for(
        exact + bad,
        provider="google_routes",
        dimension="travel_minutes",
        mode=None,
        scope="Asia/Tokyo",
        settings=SETTINGS,
    )
    estimate = calibrate(100.0, calibration)

    assert calibration.bias == pytest.approx(0.0, abs=0.01)  # it is usually right
    assert estimate.high > 105.0, "a band that ends at 103 has hidden the tail"


def test_the_band_is_asymmetric_where_the_errors_are():
    """Forcing ± around a midpoint would invent a symmetry the data lacks.
    An estimate that is occasionally far too low and never too high should
    read that way."""
    samples = [
        outcome_from_measurement(prediction(10.0, subject=f"s{i}"), actual)
        for i, actual in enumerate((10.0, 10.0, 11.0, 12.0, 18.0, 20.0))
    ]

    calibration = calibration_for(
        samples,
        provider="google_routes",
        dimension="travel_minutes",
        mode="transit",
        scope="Asia/Tokyo",
        settings=SETTINGS,
    )

    assert calibration.low_error == pytest.approx(0.0, abs=0.01)
    assert calibration.high_error > 0.5


def test_the_chain_says_which_rung_answered():
    """A bias borrowed from a provider's global record is a weaker claim than
    one measured where the question was asked, and a reader not told which they
    are holding has been misled by omission."""
    tokyo = answered(6)

    here = calibration_for(
        tokyo,
        provider="google_routes",
        dimension="travel_minutes",
        mode="transit",
        scope="Asia/Tokyo",
        settings=SETTINGS,
    )
    assert (here.level, here.scope_used) == ("scoped", "Asia/Tokyo")

    elsewhere = calibration_for(
        tokyo,
        provider="google_routes",
        dimension="travel_minutes",
        mode="transit",
        scope="America/Chicago",
        settings=SETTINGS,
    )
    assert elsewhere.level == "mode"
    assert elsewhere.scope_used == "", "a borrowed answer must not claim a place"
    assert elsewhere.bias == here.bias


def test_transit_and_driving_keep_separate_records():
    """The point of the exercise. `hotel_area_service` falls back from transit
    to driving where Google has no transit data (ledger 2), so the two are
    routinely mixed in one shortlist while being different measurements."""
    corpus = answered(6, "a_bit_longer") + answered(
        6, "about_right", mode="driving", value=20.0
    )

    def bias(mode):
        return calibration_for(
            corpus,
            provider="google_routes",
            dimension="travel_minutes",
            mode=mode,
            scope="Asia/Tokyo",
            settings=SETTINGS,
        ).bias

    assert bias("transit") > 0.2
    assert bias("driving") == pytest.approx(0.0, abs=0.01)


def test_more_evidence_promotes_it_from_provisional_to_calibrated():
    def status(n):
        return calibration_for(
            answered(n),
            provider="google_routes",
            dimension="travel_minutes",
            mode="transit",
            scope="Asia/Tokyo",
            settings=SETTINGS,
        ).status

    assert status(4) == "uncalibrated"
    assert status(6) == "provisional"
    assert status(12) == "calibrated"


def test_the_stored_figure_is_never_touched():
    """`raw` is what the provider said and what the provenance points at. The
    band is a reading of it, kept beside it."""
    calibration = calibration_for(
        answered(6),
        provider="google_routes",
        dimension="travel_minutes",
        mode="transit",
        scope="Asia/Tokyo",
        settings=SETTINGS,
    )

    estimate = calibrate(14.0, calibration)

    assert estimate.raw == 14.0
    assert estimate.low > 14.0 and estimate.high > estimate.low


def test_a_band_never_goes_below_zero():
    fast = [outcome_for(prediction(10.0, subject=f"q{i}"), answer="quicker") for i in range(6)]
    calibration = calibration_for(
        fast,
        provider="google_routes",
        dimension="travel_minutes",
        mode="transit",
        scope="Asia/Tokyo",
        settings=SETTINGS,
    )

    assert calibrate(10.0, calibration).low >= 0.0


def test_calibration_annotates_a_card_and_never_reorders_it():
    """The plan wanted a ranking correction for shortlists that mix travel
    modes. Checked against the live database, none does: twelve stored
    shortlists carry a mode and not one holds two, because a shortlist is
    measured in a single route matrix and the transit-to-driving fallback
    applies to the whole matrix at once.

    So every option on a card shares a bias, correcting them would multiply
    them all by the same number, and no order could change. The note is the
    consumer; the order is not.
    """
    from app.services.calibration_service import Calibrations
    from app.services.decision_service import decision_views

    state = trip_with(
        "hotel_area",
        area("Ginza", 14.0),
        area("Asakusa", 27.0),
        area("Shinjuku", 20.0),
    )
    corpus = Calibrations.of(answered(12), scope="Asia/Tokyo", settings=SETTINGS)

    plain = decision_views(state)
    annotated = decision_views(state, corpus)

    def order(views):
        return [o.label for v in views if v.name == "hotel_area" for o in v.options]

    assert order(annotated) == order(plain)

    notes = [
        o.metrics[0].note
        for v in annotated
        if v.name == "hotel_area"
        for o in v.options
    ]
    assert all(note and "12 checks" in note for note in notes)


# --- what gets asked ---------------------------------------------------------


def test_only_figures_that_decided_something_are_worth_asking_about():
    state = trip_with("hotel_area", area("Ginza", 14.0), area("Asakusa", 27.0), selected=0)

    asked = questions_for(state, [], settings=SETTINGS)

    assert [p.subject for p in asked] == ["hotel_area:Ginza"]
    assert "14" in question_text(asked[0])


def test_the_card_is_not_a_survey():
    state = sample_state()
    state.brief.timezone = "Asia/Tokyo"
    for name in ("hotel_area", "departure_airport", "arrival_airport"):
        payload = area(name, 14.0) if name == "hotel_area" else airport(name[:3].upper(), 30.0)
        setattr(
            state.decisions,
            name,
            Decision(
                options=[DecisionOption(option_id=f"opt_{name}", data=payload)],
                selected_option_id=f"opt_{name}",
            ),
        )

    assert len(questions_for(state, [], settings=SETTINGS)) == 2


def test_a_question_already_answered_is_not_asked_again():
    """The prediction id is a content hash, so the same estimate derives to the
    same id on every read. Without this, one journey answered twice would count
    as two."""
    state = trip_with("hotel_area", area("Ginza", 14.0), selected=0)
    only = predictions_from(state)[0]

    assert [p.prediction_id for p in questions_for(state, [], settings=SETTINGS)] == [
        only.prediction_id
    ]
    assert (
        questions_for(
            state, [], settings=SETTINGS, already_answered={only.prediction_id}
        )
        == []
    )


def test_the_least_known_key_is_asked_about_first():
    state = sample_state()
    state.brief.timezone = "Asia/Tokyo"
    state.decisions.hotel_area = Decision(
        options=[DecisionOption(option_id="opt_a", data=area("Ginza", 14.0, "transit"))],
        selected_option_id="opt_a",
    )
    state.decisions.departure_airport = Decision(
        options=[DecisionOption(option_id="opt_b", data=airport("HND", 30.0))],
        selected_option_id="opt_b",
    )

    # Driving is already well covered; transit has nothing.
    corpus = answered(9, "about_right", mode="driving", value=30.0)
    asked = questions_for(state, corpus, settings=SETTINGS, limit=1)

    assert [p.mode for p in asked] == ["transit"]


# --- the catalogue rule ------------------------------------------------------


def test_every_dimension_names_who_checks_it():
    """A dimension with no checker is a claim that can never be wrong - the
    same defect as M9's preference that influences nothing, one level up."""
    for name, entry in DIMENSIONS.items():
        assert entry.checker, f"{name} names nobody who could contradict it"
        assert entry.unit and entry.label
        if entry.checked_by == "traveller":
            assert entry.question, f"{name} needs a person and has no way to ask them"


def test_a_dimension_never_checked_says_so_rather_than_nothing():
    """Rendering nothing would let "never once checked" and "correct" look
    identical on screen."""
    assert set(dimensions_never_checked([])) == set(DIMENSIONS)

    checked = answered(1)
    assert "travel_minutes" not in dimensions_never_checked(checked)
    assert "day_high_c" in dimensions_never_checked(checked)


def test_an_evidence_line_reads_back_in_the_words_it_was_given():
    line = evidence_line(outcome_for(prediction(14.0), answer="much_longer"))

    assert "14" in line and "much longer" in line and "traveller" in line


def test_a_measured_outcome_is_a_zero_width_interval():
    outcome = outcome_from_measurement(
        prediction(29.0, mode=None, dimension="day_high_c"), 31.4, checked_by="archive"
    )

    assert outcome.actual_low == outcome.actual_high == 31.4
    assert outcome.checked_by == "archive"
