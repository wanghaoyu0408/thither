"""`Why?` for the things that are not places.

`find_option` matched on `data.entity_id`. `FlightOptionData`, `HotelAreaOption`
and `DestinationOption` have no such field and never will, so for two milestones
a flight could be recommended, selected and flown with no explanation reachable
at all - the endpoint existed and could not answer.
"""

from datetime import datetime, timedelta

from app.models.common import LatLng, Money
from app.models.decision import (
    Decision,
    DecisionOption,
    DecisionScore,
    DestinationOption,
    FlightOptionData,
    HotelAreaOption,
    HotelOptionData,
)
from app.models.hotel import HotelRating
from app.models.trip import TripState
from app.services.explanation_service import explain_option


def flight(ref: str, price: float, stops: int, minutes: int = 660) -> FlightOptionData:
    depart = datetime(2026, 8, 10, 8, 0)
    return FlightOptionData(
        provider="duffel",
        offer_ref=ref,
        live_mode=True,
        price=Money(amount=price * 2),
        price_per_person=Money(amount=price),
        origin="SFO",
        destination="OGG",
        departure_at=depart,
        arrival_at=depart + timedelta(minutes=minutes),
        duration_minutes=minutes,
        stops=stops,
        airlines=["UA"],
    )


def trip_with_flights() -> TripState:
    state = TripState.new(title="t")
    state.decisions.flights = Decision[FlightOptionData](
        options=[
            DecisionOption[FlightOptionData](
                option_id="opt_best",
                data=flight("off_a", 742.0, 0),
                status="shortlisted",
                score=DecisionScore(total=0.88, dimensions={"nonstop": 1.0}, coverage=1.0),
                pros=["nonstop", "arrives before midday"],
                cons=["86 USD more than the cheapest"],
            ),
            DecisionOption[FlightOptionData](
                option_id="opt_cheap",
                data=flight("off_b", 656.0, 1, minutes=780),
                status="candidate",
                score=DecisionScore(total=0.71, coverage=1.0),
            ),
        ],
    )
    return state


def trip_with_areas() -> TripState:
    state = TripState.new(title="t")
    state.decisions.hotel_area = Decision[HotelAreaOption](
        options=[
            DecisionOption[HotelAreaOption](
                option_id="opt_kihei",
                data=HotelAreaOption(
                    area_name="Kihei",
                    center=LatLng(lat=20.76, lng=-156.45),
                    anchor_entity_ids=["ent_a", "ent_b", "ent_c"],
                    unreachable_anchors=["ent_c"],
                    mean_minutes=19.0,
                    worst_minutes=34.0,
                    travel_mode="driving",
                    community_sentiment="positive",
                    source_urls=["https://example.test/a", "https://example.test/b"],
                ),
                status="shortlisted",
                score=DecisionScore(total=0.81, coverage=0.9),
                pros=["shortest average drive to the planned stops"],
            ),
            DecisionOption[HotelAreaOption](
                option_id="opt_lahaina",
                data=HotelAreaOption(area_name="Lahaina", mean_minutes=31.0, travel_mode="driving"),
                status="shortlisted",
                score=DecisionScore(total=0.74, coverage=0.35),
            ),
        ],
    )
    return state


# --- reach --------------------------------------------------------------------


def test_a_flight_can_be_explained_at_all():
    explanation = explain_option(trip_with_flights(), "flights", "opt_best")

    assert explanation is not None
    assert explanation.option_id == "opt_best"
    assert explanation.name == "SFO → OGG · UA · 08:00", "the departure tells two offers apart"
    assert explanation.complete is True
    assert explanation.pros and explanation.cons


def test_a_destination_option_can_be_explained():
    state = TripState.new(title="t")
    state.decisions.destination = Decision[DestinationOption](
        options=[
            DecisionOption[DestinationOption](
                option_id="opt_kona",
                data=DestinationOption(city="Kona", country="United States"),
                score=DecisionScore(total=0.7),
                pros=["warm in October"],
            )
        ]
    )

    explanation = explain_option(state, "destination", "opt_kona")

    assert explanation is not None and explanation.name == "Kona"


def test_an_unknown_option_or_decision_is_none():
    state = trip_with_flights()

    assert explain_option(state, "flights", "opt_nope") is None
    assert explain_option(state, "hotel", "opt_best") is None


# --- objective figures --------------------------------------------------------


def test_flight_metrics_come_from_the_stored_option():
    explanation = explain_option(trip_with_flights(), "flights", "opt_best")

    figures = {metric.label: metric.value for metric in explanation.metrics}
    assert figures["Price"] == "742 USD per person"
    assert figures["Stops"] == "Nonstop"
    assert figures["Duration"] == "11h 00m"


def test_a_sandbox_fare_says_it_is_not_real():
    state = trip_with_flights()
    state.decisions.flights.options[0].data.live_mode = False

    explanation = explain_option(state, "flights", "opt_best")

    sandbox = next(m for m in explanation.metrics if m.label == "Fare source")
    assert "cannot be booked" in sandbox.note


def test_area_metrics_survive_persistence_and_declare_the_mode():
    explanation = explain_option(trip_with_areas(), "hotel_area", "opt_kihei")

    figures = {metric.label: metric for metric in explanation.metrics}
    assert figures["Mean travel to your stops"].value == "19 min by driving"
    # A drive time standing in for a transit time says so.
    assert "no transit times" in figures["Mean travel to your stops"].note
    assert figures["Stops reachable"].value == "2 of 3"
    assert "no route found to 1" in figures["Stops reachable"].note
    assert figures["Community view"].value == "positive"
    assert "2 source(s)" in figures["Community view"].note


def test_an_area_names_what_it_does_not_score():
    explanation = explain_option(trip_with_areas(), "hotel_area", "opt_kihei")

    unscored = " ".join(explanation.unscored).lower()
    assert "quietness" in unscored
    assert "nightlife" in unscored
    # And it says *why*, so a gap is not mistaken for a bad result.
    assert "no provider publishes" in unscored


def test_hotel_ratings_stay_separate_and_thin_evidence_is_marked():
    state = TripState.new(title="t")
    state.decisions.hotel = Decision[HotelOptionData](
        options=[
            DecisionOption[HotelOptionData](
                option_id="opt_a",
                data=HotelOptionData(
                    provider="serpapi",
                    live_mode=True,
                    name="Hotel A",
                    lat=20.7,
                    lng=-156.4,
                    nightly_price=Money(amount=228.0),
                    ratings=[
                        HotelRating(value=4.0, type="star_category", source="google_hotels"),
                        HotelRating(
                            value=4.6,
                            type="user_rating",
                            source="google_places",
                            review_count=2300,
                        ),
                    ],
                ),
                score=DecisionScore(total=0.8),
                pros=["within budget"],
            ),
            DecisionOption[HotelOptionData](
                option_id="opt_b",
                data=HotelOptionData(
                    provider="serpapi",
                    live_mode=True,
                    name="Hotel B",
                    lat=20.7,
                    lng=-156.4,
                    nightly_price=Money(amount=174.0),
                    ratings=[
                        HotelRating(
                            value=5.0, type="user_rating", source="google_places", review_count=3
                        )
                    ],
                ),
                score=DecisionScore(total=0.6),
            ),
        ]
    )

    good = explain_option(state, "hotel", "opt_a")
    thin = explain_option(state, "hotel", "opt_b")

    labels = [metric.label for metric in good.metrics]
    assert "Hotel category" in labels and "Guest rating" in labels, "never one merged number"
    assert all(metric.note is None for metric in good.metrics if metric.label == "Guest rating")

    weak = next(m for m in thin.metrics if m.label == "Guest rating")
    assert "weak evidence" in weak.note, "5.0 from 3 reviews must not read as strong"


# --- the closest alternative ---------------------------------------------------


def test_the_runner_up_and_the_measured_gap():
    explanation = explain_option(trip_with_flights(), "flights", "opt_best")

    assert explanation.alternative is not None
    assert explanation.alternative.option_id == "opt_cheap"
    assert "86 USD more" in explanation.alternative.deltas
    assert "nonstop where the other is not" in explanation.alternative.deltas
    assert "120 min shorter in the air" in explanation.alternative.deltas


def test_an_area_alternative_reports_the_travel_difference():
    explanation = explain_option(trip_with_areas(), "hotel_area", "opt_kihei")

    assert explanation.alternative.label == "Lahaina"
    assert explanation.alternative.deltas == ["12 min closer on average"]
    # Score and coverage travel separately, so a thin runner-up looks thin.
    assert explanation.alternative.coverage == 0.35


def test_a_rejected_option_is_not_offered_as_the_alternative():
    state = trip_with_flights()
    state.decisions.flights.options[1].status = "rejected"

    explanation = explain_option(state, "flights", "opt_best")

    assert explanation.alternative is None, "the traveller already said no to it"


def test_an_option_with_no_reasoning_reports_incomplete():
    state = trip_with_flights()
    state.decisions.flights.options[0].score = None
    state.decisions.flights.options[0].pros = []
    state.decisions.flights.options[0].cons = []

    explanation = explain_option(state, "flights", "opt_best")

    assert explanation.complete is False
    assert "no ranking was recorded for this option" in explanation.missing
    assert "no trade-off was recorded for this option" in explanation.missing
