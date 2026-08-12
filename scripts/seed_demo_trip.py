"""Seed a fictional trip, for screenshots and for poking at the UI.

    python scripts/seed_demo_trip.py            # create it, print the id
    python scripts/seed_demo_trip.py --delete   # remove it again

San Francisco to New York, four days. Everything is written straight into the
store: no provider is called, no key is needed, and the result is identical on
every run. That makes it safe for documentation images - the screenshots in
`docs/images/` were taken from this trip, so nobody's real destination, date
or hotel ever reaches a public repository.

The figures are invented but *shaped* like real ones - fares that differ by
awkward amounts, a rating resting on too few reviews, an advertised rate no
booking site matches - because a screenshot where every number is round
teaches the reader the wrong thing about what this system does.
"""

import argparse
import asyncio
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.repository import TripNotFound, TripRepository  # noqa: E402
from app.db.session import get_sessionmaker  # noqa: E402
from app.models.arrival import ArrivalContext, ParkingContext  # noqa: E402
from app.models.common import Money  # noqa: E402
from app.models.decision import (  # noqa: E402
    Decision,
    DecisionOption,
    DecisionScore,
    FlightOptionData,
    HotelAreaOption,
    HotelOptionData,
)
from app.models.entity import PlaceEntity  # noqa: E402
from app.models.flight import AirportOption, FlightSegment, FlightSlice  # noqa: E402
from app.models.hotel import HotelPriceQuote, HotelRating  # noqa: E402
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary  # noqa: E402
from app.models.lock import LockRecord  # noqa: E402
from app.models.trip import (  # noqa: E402
    DestinationSpec,
    OriginSpec,
    PartySpec,
    TripBrief,
    TripDates,
    TripIntake,
    TripState,
    TripTraveler,
)
from app.models.weather import WeatherContext  # noqa: E402

TRIP_ID = "trip_demo_nyc"
START = date(2026, 10, 15)
DAYS = 4
ZONE = "America/New_York"

# The landmarks, with their real Google place ids, coordinates and ratings -
# resolved once through this project's own Places search and then written down
# here, so the script itself stays keyless and identical on every run.
#
# The ids matter beyond authenticity: the UI loads a place's photograph with
# `new Place({ id })` against the Maps JavaScript library, so an entity with no
# `provider_refs.google_place_id` renders as a lettered square. A demo whose
# cards are all grey squares does not show anyone what the interface looks
# like. These are public identifiers for public landmarks; the *itinerary* is
# the fictional part.
PLACES = [
    # entity_id, google_place_id, name, categories, lat, lng, rating, reviews
    ("ent_met", "ChIJb8Jg9pZYwokR-qHGtvSkLzs", "The Metropolitan Museum of Art",
     ["museum"], 40.7794, -73.9632, 4.8, 94451),
    ("ent_central_park", "ChIJ4zGFAZpYwokRGUGph3Mf37k", "Central Park",
     ["park"], 40.7826, -73.9656, 4.8, 300880),
    ("ent_highline", "ChIJ5bQPhMdZwokRkTwKhVxhP1g", "The High Line",
     ["park"], 40.7480, -74.0048, 4.7, 67775),
    ("ent_katz", "ChIJCar0f49ZwokR6ozLV-dHNTE", "Katz's Delicatessen",
     ["restaurant"], 40.7222, -73.9874, 4.5, 54627),
    ("ent_moma", "ChIJKxDbe_lYwokRVf__s8CPn-o", "The Museum of Modern Art",
     ["museum"], 40.7614, -73.9776, 4.6, 60517),
    ("ent_dumbo", "ChIJjaFpo0ZawokRBcOFUZ13CaE", "Brooklyn Bridge Park",
     ["park"], 40.7022, -73.9959, 4.8, 43237),
    ("ent_grand", "ChIJhRwB-yFawokRi0AhGH87UTc", "Grand Central",
     ["tourist_attraction"], 40.7528, -73.9772, 4.7, 7866),
    ("ent_theatre", "ChIJtehhClVYwokRjEHM1kjDj2M", "Lyceum Theatre",
     ["performing_arts_theater"], 40.7577, -73.9846, 4.6, 2285),
    ("ent_joes", "ChIJ8Q2WSpJZwokRQz-bYYgEskM", "Joe's Pizza",
     ["restaurant"], 40.7307, -74.0022, 4.4, 10507),
    ("ent_russ", "ChIJ53yq2oZZwokRqIHSP4qZT3o", "Russ & Daughters Cafe",
     ["restaurant"], 40.7196, -73.9896, 4.6, 3683),
]


def entities() -> dict[str, PlaceEntity]:
    return {
        eid: PlaceEntity(
            entity_id=eid,
            name=name,
            categories=cats,
            lat=lat,
            lng=lng,
            rating=rating,
            rating_count=reviews,
            address=f"{name}, New York, NY",
            timezone=ZONE,
            price_level=2,
            provider_refs={"google_place_id": place_id},
        )
        for eid, place_id, name, cats, lat, lng, rating, reviews in PLACES
    }


def at(day_offset: int, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(START + timedelta(days=day_offset), time(hh, mm))


def item(item_id, kind, entity_id, title, day, start, end, **fields) -> ItineraryItem:
    return ItineraryItem(
        item_id=item_id,
        type=kind,
        entity_id=entity_id,
        title=title,
        start_at=at(day, *start),
        end_at=at(day, *end),
        **fields,
    )


def forecast(day_offset: int, condition: str, high: float, low: float, rain: float, wind: float):
    when = START + timedelta(days=day_offset)
    return WeatherContext(
        date=when,
        kind="forecast",
        condition=condition,
        high_c=high,
        low_c=low,
        precipitation_probability=rain,
        wind_kph=wind,
        source="google_weather",
        # Mid-October in New York: the sun is down not long after six. Stored
        # as UTC, the way the provider gives it.
        sunset=datetime.combine(when, time(22, 12)),
        sunrise=datetime.combine(when, time(11, 8)),
    )


def itinerary() -> TripItinerary:
    return TripItinerary(
        generated_at=datetime(2026, 9, 20, 11, 0),
        days=[
            ItineraryDay(
                date=START,
                theme="Land, then walk it off downtown",
                weather=forecast(0, "Clear", 19.0, 11.0, 0.06, 14.0),
                items=[
                    item("item_arrive", "flight", None, "SFO → JFK, lands 17:05",
                         0, (8, 30), (17, 5), time_flexibility="fixed"),
                    item("item_checkin", "hotel", None, "Check in, Lower East Side",
                         0, (18, 30), (19, 0)),
                    item("item_katz", "restaurant", "ent_katz", "Late dinner at Katz's",
                         0, (19, 30), (20, 45), estimated_cost=Money(amount=62.0)),
                ],
            ),
            ItineraryDay(
                date=START + timedelta(days=1),
                theme="Uptown museums, then a show",
                weather=forecast(1, "Clear", 18.0, 10.0, 0.11, 12.0),
                items=[
                    item("item_russ", "restaurant", "ent_russ", "Breakfast at Russ & Daughters",
                         1, (9, 30), (10, 30), estimated_cost=Money(amount=48.0)),
                    item("item_met", "activity", "ent_met", "The Met", 1, (11, 30), (14, 30)),
                    item("item_park", "activity", "ent_central_park",
                         "Walk down through Central Park", 1, (14, 45), (16, 15)),
                    # The interesting one: a curtain that does not wait.
                    item("item_show", "activity", "ent_theatre", "Broadway, 19:00 curtain",
                         1, (19, 0), (21, 40),
                         time_flexibility="fixed", reservation_required=True,
                         reservation_booked=True, estimated_cost=Money(amount=340.0)),
                ],
            ),
            ItineraryDay(
                date=START + timedelta(days=2),
                theme="Downtown and Brooklyn, weather permitting",
                # Rain on the day with two outdoor stops, so the stress test
                # has something true to say about it.
                weather=forecast(2, "Rain", 15.0, 9.0, 0.71, 26.0),
                items=[
                    item("item_joes", "restaurant", "ent_joes", "Slice at Joe's",
                         2, (12, 0), (12, 40), estimated_cost=Money(amount=18.0)),
                    item("item_highline", "activity", "ent_highline", "The High Line",
                         2, (13, 15), (14, 45)),
                    item("item_dumbo", "activity", "ent_dumbo", "Brooklyn Bridge Park at dusk",
                         2, (16, 30), (18, 30)),
                ],
            ),
            ItineraryDay(
                date=START + timedelta(days=3),
                theme="Midtown, then the airport",
                weather=forecast(3, "Partly cloudy", 17.0, 10.0, 0.22, 15.0),
                items=[
                    item("item_moma", "activity", "ent_moma", "MoMA", 3, (10, 0), (12, 30)),
                    item("item_grand", "activity", "ent_grand",
                         "Grand Central, and lunch in the market", 3, (12, 50), (14, 0)),
                    item("item_depart", "flight", None, "JFK → SFO, 18:40",
                         3, (16, 30), (22, 15), time_flexibility="fixed"),
                ],
            ),
        ],
    )


def leg(origin, destination, depart, arrive, stops, carrier):
    """A slice with real segments - `FlightSlice.stops` counts them."""
    hops = stops + 1
    span = (arrive - depart) / hops
    return FlightSlice(
        origin=origin,
        destination=destination,
        departing_at=depart,
        arriving_at=arrive,
        duration_minutes=int((arrive - depart).total_seconds() // 60),
        segments=[
            FlightSegment(
                origin=origin if i == 0 else "ORD",
                destination=destination if i == hops - 1 else "ORD",
                departing_at=depart + span * i,
                arriving_at=depart + span * (i + 1),
                marketing_carrier=carrier,
                flight_number=f"{carrier}{615 + i}",
            )
            for i in range(hops)
        ],
    )


def flight(ref, price, stops, out_depart, out_arrive, back_depart, back_arrive, carrier):
    return FlightOptionData(
        provider="duffel",
        offer_ref=ref,
        live_mode=True,
        price=Money(amount=price * 2),
        price_per_person=Money(amount=price),
        origin="SFO",
        destination="JFK",
        departure_at=out_depart,
        arrival_at=out_arrive,
        duration_minutes=int((out_arrive - out_depart).total_seconds() // 60),
        stops=stops,
        airlines=[carrier],
        cabin="economy",
        slices=[
            leg("SFO", "JFK", out_depart, out_arrive, stops, carrier),
            leg("JFK", "SFO", back_depart, back_arrive, stops, carrier),
        ],
    )


def flights_decision() -> Decision:
    return Decision(
        decision_id="dec_demo_flights",
        status="shortlisted",
        options=[
            DecisionOption(
                option_id="opt_nonstop",
                data=flight("off_b6", 412.0, 0,
                            at(0, 8, 30), at(0, 17, 5),
                            at(3, 18, 40), at(3, 22, 15), "B6"),
                score=DecisionScore(total=0.82, coverage=1.0),
                pros=["nonstop both ways", "lands with the evening still ahead of you"],
                cons=["$97 more per person than the connection"],
            ),
            DecisionOption(
                option_id="opt_connection",
                data=flight("off_ua", 315.0, 1,
                            at(0, 6, 15), at(0, 18, 40),
                            at(3, 17, 5), at(3, 23, 50), "UA"),
                score=DecisionScore(total=0.61, coverage=1.0),
                pros=["cheapest fare on the route"],
                cons=["one stop each way", "06:15 departure, and lands after dark"],
            ),
        ],
    )


def airport(iata, name, city, lat, lng, minutes, distance, source="routes_api"):
    return AirportOption(
        iata=iata,
        name=name,
        city=city,
        lat=lat,
        lng=lng,
        ground_travel_minutes=minutes,
        ground_travel_source=source,
        distance_km=distance,
    )


def departure_airport_decision() -> Decision:
    return Decision(
        decision_id="dec_demo_dep",
        status="selected",
        selected_option_id="opt_sfo",
        options=[
            DecisionOption(
                option_id="opt_sfo",
                data=airport("SFO", "San Francisco International", "San Francisco",
                             37.6213, -122.3790, 31.4, 21.3),
                score=DecisionScore(total=0.88, coverage=1.0),
                pros=["31.4 min drive", "the only one with a nonstop on these dates"],
            ),
            DecisionOption(
                option_id="opt_oak",
                data=airport("OAK", "Oakland International", "Oakland",
                             37.7213, -122.2210, 42.9, 29.8),
                score=DecisionScore(total=0.54, coverage=1.0),
                pros=["quieter terminal"],
                cons=["42.9 min drive", "one stop each way from here"],
            ),
            DecisionOption(
                option_id="opt_sjc",
                data=airport("SJC", "San José Mineta", "San José",
                             37.3639, -121.9290, None, 74.1, source="not_looked_up"),
                cons=["drive time was not measured, so this airport cannot be compared on access"],
            ),
        ],
    )


def hotel_area_decision() -> Decision:
    anchors = ["ent_met", "ent_theatre", "ent_dumbo", "ent_highline"]
    return Decision(
        decision_id="dec_demo_area",
        status="shortlisted",
        options=[
            DecisionOption(
                option_id="opt_les",
                data=HotelAreaOption(
                    area_name="Lower East Side",
                    mean_minutes=24.6,
                    worst_minutes=38.0,
                    travel_mode="transit",
                    anchor_entity_ids=anchors,
                    community_sentiment="positive",
                    source_urls=["https://example.invalid/a", "https://example.invalid/b"],
                ),
                score=DecisionScore(total=0.77, coverage=1.0),
                pros=["the food you came for is downstairs", "24.6 min to your stops on average"],
                cons=["loud on a Friday night"],
            ),
            DecisionOption(
                option_id="opt_midtown",
                data=HotelAreaOption(
                    area_name="Midtown East",
                    mean_minutes=18.2,
                    worst_minutes=31.0,
                    travel_mode="transit",
                    anchor_entity_ids=anchors,
                    community_sentiment="mixed",
                    source_urls=["https://example.invalid/c"],
                ),
                score=DecisionScore(total=0.81, coverage=1.0),
                pros=["closest to everything on the plan", "eight minutes from the theatre"],
                cons=["empties out after seven"],
            ),
            DecisionOption(
                option_id="opt_williamsburg",
                data=HotelAreaOption(
                    area_name="Williamsburg",
                    mean_minutes=41.3,
                    worst_minutes=63.0,
                    travel_mode="transit",
                    anchor_entity_ids=anchors,
                    unreachable_anchors=["ent_met"],
                    community_sentiment="unclear",
                ),
                score=DecisionScore(
                    total=0.42, coverage=0.71, notes="scored on 71% of the usual evidence"
                ),
                pros=["cheaper rooms", "the bar street is right there"],
                cons=["41.3 min to the rest of the trip", "no transit route found to 1 stop"],
            ),
        ],
    )


def hotel_decision() -> Decision:
    return Decision(
        decision_id="dec_demo_hotel",
        status="shortlisted",
        options=[
            DecisionOption(
                option_id="opt_hotel_a",
                data=HotelOptionData(
                    provider="serpapi_google_hotels",
                    live_mode=True,
                    name="The Ludlow House",
                    entity_id="ent_hotel_a",
                    area_name="Lower East Side",
                    nightly_price=Money(amount=287.0),
                    headline_nightly=Money(amount=249.0),
                    quotes=[
                        HotelPriceQuote(source="a booking site", nightly=Money(amount=287.0)),
                        HotelPriceQuote(source="another site", nightly=Money(amount=299.0)),
                    ],
                    ratings=[
                        HotelRating(
                            type="user_rating",
                            source="google_hotels",
                            value=4.5,
                            review_count=2180,
                        ),
                        HotelRating(type="star_category", source="google_hotels", value=4.0),
                    ],
                    route_minutes={"ent_katz": 6.0, "ent_theatre": 22.0},
                    route_mode="transit",
                ),
                score=DecisionScore(total=0.79, coverage=1.0),
                pros=["six minutes from Katz's", "4.5 from 2,180 guests"],
                cons=["the advertised $249 is not matched by any site we can name"],
            ),
            DecisionOption(
                option_id="opt_hotel_b",
                data=HotelOptionData(
                    provider="serpapi_google_hotels",
                    live_mode=True,
                    name="Vanderbilt Court",
                    entity_id="ent_hotel_b",
                    area_name="Midtown East",
                    nightly_price=Money(amount=341.0),
                    quotes=[
                        HotelPriceQuote(source="a booking site", nightly=Money(amount=341.0))
                    ],
                    ratings=[
                        HotelRating(
                            type="user_rating",
                            source="google_hotels",
                            value=4.9,
                            review_count=31,
                        )
                    ],
                    route_minutes={"ent_theatre": 8.0, "ent_moma": 11.0},
                    route_mode="transit",
                ),
                score=DecisionScore(total=0.68, coverage=0.8),
                pros=["eight minutes from the theatre"],
                cons=["4.9 rests on 31 reviews, which is too few to lean on"],
            ),
        ],
    )


def demo_state() -> TripState:
    state = TripState.new(title="New York, four days", created_by="demo")
    state.trip_id = TRIP_ID
    state.status = "planning"
    state.brief = TripBrief(
        origin=OriginSpec(city="San Francisco", airport_codes=["SFO", "OAK", "SJC"]),
        destination=DestinationSpec(city="New York", country="United States", flexible=False),
        dates=TripDates(start=START, end=START + timedelta(days=DAYS - 1)),
        party=PartySpec(adults=2, rooms=1),
        timezone=ZONE,
        pace="balanced",
        priorities=["museums", "food", "one show"],
        notes="Two of us. No early starts, and we want one proper night out.",
    )
    state.travelers = [TripTraveler(traveler_id="trv_demo", name="Sam", role="organizer")]
    # Confirmed, because this trip is meant to look like one that has been
    # planned rather than one still being briefed - otherwise the intake card
    # owns the screen and the itinerary never gets a look in.
    state.intake = TripIntake(
        status="confirmed",
        confirmed_brief=state.brief,
        confirmed_revision=0,
        confirmed_at=datetime(2026, 9, 20, 10, 40),
    )
    state.entities = entities()
    state.itinerary = itinerary()
    state.decisions.departure_airport = departure_airport_decision()
    state.decisions.flights = flights_decision()
    state.decisions.hotel_area = hotel_area_decision()
    state.decisions.hotel = hotel_decision()
    state.locks = [
        LockRecord(
            lock_id="lock_demo_show",
            target_kind="itinerary_item",
            target_id="item_show",
            reason="the tickets are booked and the curtain does not wait",
        )
    ]
    # Somewhere nobody checked. On a transit trip it changes no arithmetic; it
    # is here because "unknown" is a state the interface has to be able to
    # show, and a demo where everything is known teaches the wrong lesson.
    state.arrival["ent_dumbo"] = ArrivalContext(
        entity_id="ent_dumbo",
        mode="unknown",
        parking=ParkingContext(
            availability="unknown",
            notes="nobody has looked up how you would arrive here by car",
        ),
    )
    return state


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true", help="remove the demo trip")
    args = parser.parse_args()

    sessions = get_sessionmaker()
    async with sessions() as session:
        repo = TripRepository(session)
        # The Lisbon id is the one this script used to seed; clearing it keeps
        # an older checkout from leaving a second demo trip behind.
        for old in (TRIP_ID, "trip_demo_lisbon"):
            try:
                await repo.delete(old)
                print(f"removed {old}")
            except TripNotFound:
                pass
        if args.delete:
            return

        stored = await repo.create(demo_state())
        print(f"created {stored.trip_id}  ·  {stored.metadata.title}")
        print(
            f"  {len(stored.entities)} places, "
            f"{len(stored.itinerary.days)} days, "
            f"{sum(len(d.items) for d in stored.itinerary.days)} stops, "
            f"{len(stored.decisions.iter_decisions())} decisions, "
            f"{len(stored.locks)} lock"
        )
        print("\nOpen http://127.0.0.1:8000 and pick 'New York, four days'.")
        print("Delete it again with:  python scripts/seed_demo_trip.py --delete")


if __name__ == "__main__":
    asyncio.run(main())
