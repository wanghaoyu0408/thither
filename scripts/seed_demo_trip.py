"""Seed a fictional trip, for screenshots and for poking at the UI.

    python scripts/seed_demo_trip.py            # create it, print the id
    python scripts/seed_demo_trip.py --delete   # remove it again

Everything is written straight into the store: no provider is called, no key
is needed, and the result is byte-identical on every run. That makes it safe
for documentation images - the screenshots in `docs/images/` were taken from
this trip, so no real destination, date or hotel of anyone's ever reaches a
public repository.

The figures are invented but *shaped* like real ones, because a screenshot of
a plan with round numbers everywhere teaches the reader the wrong thing about
what this system does.
"""

import argparse
import asyncio
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.repository import TripNotFound, TripRepository  # noqa: E402
from app.db.session import get_sessionmaker  # noqa: E402
from app.models.arrival import ArrivalContext, ParkingContext, ParkingSpot  # noqa: E402
from app.models.common import Money  # noqa: E402
from app.models.decision import (  # noqa: E402
    Decision,
    DecisionOption,
    DecisionScore,
    HotelAreaOption,
    HotelOptionData,
)
from app.models.entity import PlaceEntity  # noqa: E402
from app.models.hotel import HotelPriceQuote, HotelRating  # noqa: E402
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary  # noqa: E402
from app.models.lock import LockRecord  # noqa: E402
from app.models.trip import (  # noqa: E402
    DestinationSpec,
    OriginSpec,
    PartySpec,
    TripBrief,
    TripDates,
    TripState,
    TripTraveler,
)
from app.models.weather import WeatherContext  # noqa: E402

TRIP_ID = "trip_demo_lisbon"
START = date(2026, 9, 18)
ZONE = "Europe/Lisbon"

PLACES = [
    # entity_id, name, categories, lat, lng, rating, reviews
    ("ent_jeronimos", "Jerónimos Monastery", ["tourist_attraction"], 38.6979, -9.2065, 4.7, 41320),
    ("ent_belem", "Pastéis de Belém", ["bakery", "cafe"], 38.6975, -9.2033, 4.5, 62840),
    ("ent_gulbenkian", "Gulbenkian Garden", ["garden", "park"], 38.7377, -9.1540, 4.8, 9120),
    ("ent_timeout", "Time Out Market", ["restaurant", "food"], 38.7071, -9.1459, 4.4, 88210),
    ("ent_miradouro", "Miradouro da Senhora do Monte", ["park"], 38.7185, -9.1330, 4.7, 15640),
    ("ent_alfama", "A Baiuca", ["restaurant"], 38.7118, -9.1281, 4.6, 1830),
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
            address=f"{name}, Lisbon",
            timezone=ZONE,
            price_level=2,
        )
        for eid, name, cats, lat, lng, rating, reviews in PLACES
    }


def at(day_offset: int, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(START + timedelta(days=day_offset), time(hh, mm))


def itinerary() -> TripItinerary:
    day_one = ItineraryDay(
        date=START,
        theme="Belém, slowly",
        weather=WeatherContext(
            date=START,
            kind="forecast",
            condition="Light rain",
            high_c=23.0,
            low_c=17.0,
            precipitation_probability=0.62,
            wind_kph=18.0,
            source="google_weather",
            sunset=datetime(2026, 9, 18, 18, 52),
        ),
        items=[
            ItineraryItem(
                item_id="item_jeronimos",
                type="activity",
                entity_id="ent_jeronimos",
                title="Jerónimos Monastery",
                start_at=at(0, 10, 0),
                end_at=at(0, 11, 40),
            ),
            ItineraryItem(
                item_id="item_belem",
                type="restaurant",
                entity_id="ent_belem",
                title="Pastéis de Belém",
                start_at=at(0, 12, 0),
                end_at=at(0, 12, 50),
                estimated_cost=Money(amount=14.0, currency="EUR"),
            ),
            ItineraryItem(
                item_id="item_gulbenkian",
                type="activity",
                entity_id="ent_gulbenkian",
                title="Gulbenkian Garden",
                start_at=at(0, 15, 0),
                end_at=at(0, 17, 0),
            ),
            ItineraryItem(
                item_id="item_timeout",
                type="restaurant",
                entity_id="ent_timeout",
                title="Dinner at Time Out Market",
                start_at=at(0, 19, 30),
                end_at=at(0, 21, 0),
                estimated_cost=Money(amount=68.0, currency="EUR"),
                reservation_required=True,
            ),
        ],
    )
    day_two = ItineraryDay(
        date=START + timedelta(days=1),
        theme="Alfama and the miradouros",
        weather=WeatherContext(
            date=START + timedelta(days=1),
            kind="forecast",
            condition="Clear",
            high_c=26.0,
            low_c=18.0,
            precipitation_probability=0.08,
            wind_kph=11.0,
            source="google_weather",
            sunset=datetime(2026, 9, 19, 18, 50),
        ),
        items=[
            ItineraryItem(
                item_id="item_miradouro",
                type="activity",
                entity_id="ent_miradouro",
                title="Miradouro da Senhora do Monte",
                start_at=at(1, 17, 15),
                end_at=at(1, 18, 30),
            ),
            ItineraryItem(
                item_id="item_alfama",
                type="restaurant",
                entity_id="ent_alfama",
                title="Fado dinner at A Baiuca",
                start_at=at(1, 20, 0),
                end_at=at(1, 22, 30),
                estimated_cost=Money(amount=95.0, currency="EUR"),
                time_flexibility="fixed",
                reservation_required=True,
                reservation_booked=True,
            ),
        ],
    )
    return TripItinerary(days=[day_one, day_two], generated_at=datetime(2026, 8, 1, 9, 0))


def hotel_area_decision() -> Decision:
    return Decision(
        decision_id="dec_demo_area",
        status="shortlisted",
        options=[
            DecisionOption(
                option_id="opt_alfama",
                data=HotelAreaOption(
                    area_name="Alfama",
                    mean_minutes=13.4,
                    worst_minutes=27.0,
                    travel_mode="transit",
                    anchor_entity_ids=["ent_timeout", "ent_miradouro", "ent_gulbenkian"],
                    community_sentiment="positive",
                    source_urls=["https://example.invalid/a", "https://example.invalid/b"],
                ),
                score=DecisionScore(total=0.79, coverage=1.0),
                pros=["13 min to your stops on average", "walkable to the fado houses"],
                cons=["steep streets and cobbles"],
            ),
            DecisionOption(
                option_id="opt_chiado",
                data=HotelAreaOption(
                    area_name="Chiado",
                    mean_minutes=9.8,
                    worst_minutes=19.0,
                    travel_mode="transit",
                    anchor_entity_ids=["ent_timeout", "ent_miradouro", "ent_gulbenkian"],
                    community_sentiment="mixed",
                    source_urls=["https://example.invalid/c"],
                ),
                score=DecisionScore(total=0.83, coverage=1.0),
                pros=["closest to everything on the plan"],
                cons=["busiest at night"],
            ),
            DecisionOption(
                option_id="opt_belem",
                data=HotelAreaOption(
                    area_name="Belém",
                    mean_minutes=31.2,
                    worst_minutes=48.0,
                    travel_mode="driving",
                    anchor_entity_ids=["ent_timeout", "ent_miradouro", "ent_gulbenkian"],
                    unreachable_anchors=["ent_miradouro"],
                    community_sentiment="unclear",
                ),
                score=DecisionScore(
                    total=0.44, coverage=0.66, notes="scored on 66% of the usual evidence"
                ),
                pros=["quiet in the evening"],
                cons=["31 min to the rest of the trip", "no route found to 1 stop"],
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
                    name="Casa das Janelas",
                    entity_id="ent_hotel_a",
                    area_name="Alfama",
                    nightly_price=Money(amount=148.0, currency="EUR"),
                    headline_nightly=Money(amount=129.0, currency="EUR"),
                    quotes=[
                        HotelPriceQuote(
                            source="a booking site",
                            nightly=Money(amount=148.0, currency="EUR"),
                        ),
                        HotelPriceQuote(
                            source="another site",
                            nightly=Money(amount=155.0, currency="EUR"),
                        ),
                    ],
                    ratings=[
                        HotelRating(
                            type="user_rating",
                            source="google_hotels",
                            value=4.6,
                            review_count=1240,
                        ),
                        HotelRating(type="star_category", source="google_hotels", value=4.0),
                    ],
                    route_minutes={"ent_timeout": 11.0, "ent_miradouro": 8.0},
                    route_mode="walking",
                ),
                score=DecisionScore(total=0.81, coverage=1.0),
                pros=["8 min walk to the viewpoint", "4.6 from 1,240 guests"],
                cons=["advertised rate is 19 EUR below any quote anyone can be named for"],
            ),
            DecisionOption(
                option_id="opt_hotel_b",
                data=HotelOptionData(
                    provider="serpapi_google_hotels",
                    live_mode=True,
                    name="Pátio do Chiado",
                    entity_id="ent_hotel_b",
                    area_name="Chiado",
                    nightly_price=Money(amount=196.0, currency="EUR"),
                    quotes=[
                        HotelPriceQuote(
                            source="a booking site",
                            nightly=Money(amount=196.0, currency="EUR"),
                        )
                    ],
                    ratings=[
                        HotelRating(
                            type="user_rating", source="google_hotels", value=4.8, review_count=38
                        )
                    ],
                    route_minutes={"ent_timeout": 5.0, "ent_miradouro": 16.0},
                    route_mode="walking",
                ),
                score=DecisionScore(total=0.74, coverage=0.8),
                pros=["5 min from the market"],
                cons=["4.8 rests on 38 reviews, which is thin"],
            ),
        ],
    )


def demo_state() -> TripState:
    state = TripState.new(title="Lisbon, four days", created_by="demo")
    state.trip_id = TRIP_ID
    state.status = "planning"
    state.brief = TripBrief(
        origin=OriginSpec(city="Berlin", airport_codes=["BER"]),
        destination=DestinationSpec(city="Lisbon", country="Portugal", flexible=False),
        dates=TripDates(start=START, end=START + timedelta(days=3)),
        party=PartySpec(adults=2, rooms=1),
        timezone=ZONE,
        pace="balanced",
        priorities=["food", "walking", "one museum"],
        notes="Somewhere with a view for the last night. No early starts.",
    )
    state.travelers = [
        TripTraveler(traveler_id="trv_demo", name="Sam", role="organizer"),
    ]
    state.entities = entities()
    state.itinerary = itinerary()
    state.decisions.hotel_area = hotel_area_decision()
    state.decisions.hotel = hotel_decision()
    state.locks = [
        LockRecord(
            lock_id="lock_demo_fado",
            target_kind="itinerary_item",
            target_id="item_alfama",
            reason="the fado table is booked and paid for",
        )
    ]
    # One place whose parking nobody checked, so the stress test has something
    # honest to be uncertain about.
    state.arrival["ent_gulbenkian"] = ArrivalContext(
        entity_id="ent_gulbenkian",
        mode="driving",
        parking=ParkingContext(
            availability="unknown",
            notes="no parking information published for this entrance",
        ),
    )
    state.arrival["ent_jeronimos"] = ArrivalContext(
        entity_id="ent_jeronimos",
        mode="driving",
        parking=ParkingContext(
            availability="likely",
            spots=[
                ParkingSpot(
                    kind="parking_lot",
                    cost="paid",
                    name="Belém riverside car park",
                    walking_minutes=6.0,
                    walking_meters=430,
                )
            ],
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
        try:
            await repo.delete(TRIP_ID)
            print(f"removed {TRIP_ID}")
        except TripNotFound:
            if args.delete:
                print(f"{TRIP_ID} was not there")
        if args.delete:
            return

        stored = await repo.create(demo_state())
        print(f"created {stored.trip_id}")
        print(f"  {len(stored.entities)} places, "
              f"{len(stored.itinerary.days)} days, "
              f"{sum(len(d.items) for d in stored.itinerary.days)} stops, "
              f"{len(stored.locks)} lock")
        print("\nOpen http://127.0.0.1:8000 and pick 'Lisbon, four days'.")
        print("Delete it again with:  python scripts/seed_demo_trip.py --delete")


if __name__ == "__main__":
    asyncio.run(main())
