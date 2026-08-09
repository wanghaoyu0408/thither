"""Milestone 7 acceptance: four friends who do not want the same trip.

    .\\.venv\\Scripts\\python.exe scripts\\plan_for_the_group.py

Five parts:

    1. Who is coming, and what each of them actually wants.
    2. The conflicts, with both sides stated - never one averaged number.
    3. Real hotels scored per person, showing what a mean would have hidden.
    4. Real restaurants checked against Cy's diet using live Google data.
    5. What the trip is still not allowed to claim, and why.

Needs GOOGLE_MAPS_API_KEY. SERPAPI_API_KEY adds real hotel prices to part three;
without it that part runs on fixtures and says so.
"""

import asyncio
import sys
from datetime import date, timedelta

from app.config import get_settings
from app.models.common import Money
from app.models.decision import HotelOptionData
from app.models.hotel import HotelRating, SearchHotelsInput
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary
from app.models.place import GetPlaceDetailsInput, PlaceFieldSet, SearchPlacesInput
from app.models.traveler import (
    ActivityPreferences,
    FlightPreferences,
    FoodPreferences,
    HotelPreferences,
    PacePreferences,
    TravelerProfile,
)
from app.models.trip import (
    DestinationSpec,
    TripBrief,
    TripDates,
    TripState,
    TripTraveler,
)
from app.services.conflict_service import detect_conflicts, unresolved_blocking
from app.services.entity_service import resolve_places
from app.services.group_scoring import rank_hotels_for_group
from app.services.preference_service import resolve, traveler_names
from app.services.toolbox import MissingCredentials, Toolbox

# Real Tokyo restaurant names are Japanese, and Windows pipes stdout as cp1252,
# which cannot encode them. Without this the script dies on its own output the
# moment it is redirected anywhere.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CHECK_IN = date.today() + timedelta(days=45)
CHECK_OUT = CHECK_IN + timedelta(days=4)

PROFILES = {
    "trv_ann": TravelerProfile(
        profile_id="u_ann",
        revision=1,
        name="Ann",
        flight_preferences=FlightPreferences(price_importance=0.9, nonstop_importance=0.2),
        hotel_preferences=HotelPreferences(price_importance=0.9, location_importance=0.3),
        activity_preferences=ActivityPreferences(interests=["nightlife", "shopping"]),
        pace_preferences=PacePreferences(intensity="packed", max_daily_walking_km=16.0),
    ),
    "trv_bo": TravelerProfile(
        profile_id="u_bo",
        revision=1,
        name="Bo",
        flight_preferences=FlightPreferences(price_importance=0.1, nonstop_importance=0.9),
        hotel_preferences=HotelPreferences(price_importance=0.1, location_importance=0.9),
        pace_preferences=PacePreferences(intensity="balanced"),
    ),
    "trv_cy": TravelerProfile(
        profile_id="u_cy",
        revision=1,
        name="Cy",
        food_preferences=FoodPreferences(dietary_restrictions=["vegetarian"]),
        activity_preferences=ActivityPreferences(interests=["museums"], avoided=["nightlife"]),
        pace_preferences=PacePreferences(intensity="relaxed", max_daily_walking_km=5.0),
    ),
    "trv_dee": TravelerProfile(
        profile_id="u_dee",
        revision=1,
        name="Dee",
        hotel_preferences=HotelPreferences(min_rating=4.5, quiet_importance=0.9),
        pace_preferences=PacePreferences(preferred_start_time="11:00"),
    ),
}

FIXTURE_HOTELS = [
    ("Bargain Inn Ueno", 78.0, 3.2, 900),
    ("Mid Range Ueno", 132.0, 4.6, 2100),
    ("Quiet House Yanaka", 168.0, 4.7, 640),
]


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'-' * 78}")


def group_trip() -> TripState:
    state = TripState.new(title="four friends in Tokyo")
    state.brief = TripBrief(
        destination=DestinationSpec(city="Tokyo", country="Japan", flexible=False),
        timezone="Asia/Tokyo",
        dates=TripDates(start=CHECK_IN, end=CHECK_OUT),
    )
    state.travelers = []
    for traveler_id, profile in PROFILES.items():
        traveler = TripTraveler(
            traveler_id=traveler_id,
            name=profile.name,
            profile_id=profile.profile_id,
            role="organizer" if traveler_id == "trv_ann" else "member",
        )
        # The snapshot. Planning reads this and never the profile again.
        traveler.preferences = resolve(traveler, profile)
        state.travelers.append(traveler)
    return state


def fixture_hotels() -> list[HotelOptionData]:
    return [
        HotelOptionData(
            provider="fixture",
            offer_ref=name.lower().replace(" ", "_"),
            live_mode=True,
            name=name,
            nightly_price=Money(amount=price),
            ratings=[
                HotelRating(
                    value=rating,
                    type="user_rating",
                    source="google_hotels",
                    review_count=reviews,
                )
            ],
        )
        for name, price, rating, reviews in FIXTURE_HOTELS
    ]


def show_group(state: TripState, options: list[HotelOptionData], source: str) -> None:
    travelers = {t.traveler_id: t.preferences for t in state.travelers}
    names = traveler_names(state)
    ranked = rank_hotels_for_group(options, travelers=travelers, names=names)

    print(f"    scored on {source}\n")
    for item in ranked:
        price = item.option.nightly_price
        rating = item.option.user_rating
        print(f"    {item.option.name}")
        print(
            f"       {price.amount:.0f} {price.currency}/night"
            + (f"   {rating.value:g}/5 from {rating.review_count:,} reviews" if rating else "")
        )
        # The group verdict, which cannot be printed without its split.
        print(f"       group: {item.group.describe()}")
        per_person = "  ".join(
            f"{names[tid]} {value:.2f}" for tid, value in sorted(item.group.per_traveler.items())
        )
        print(f"       each:  {per_person}")
        for con in item.cons[:2]:
            print(f"       - {con}")
        print()

    best = ranked[0]
    mean_pick = max(ranked, key=lambda item: item.group.mean)
    if mean_pick.option.name != best.option.name:
        print(
            f"    A plain average would have recommended {mean_pick.option.name} "
            f"(mean {mean_pick.group.mean:.2f} against {best.group.mean:.2f}).\n"
            f"    It is not recommended, because it leaves "
            f"{mean_pick.group.name_of(mean_pick.group.worst_traveler_id)} at "
            f"{mean_pick.group.worst:.2f}."
        )
    else:
        print(f"    Recommended: {best.option.name} - {best.group.describe()}")


async def main() -> int:
    settings = get_settings()
    if not settings.google_maps_api_key:
        print("\nMissing GOOGLE_MAPS_API_KEY in .env\n")
        return 1

    state = group_trip()

    try:
        async with Toolbox(settings) as toolbox:
            # --- 1. Who wants what ------------------------------------------
            banner("1. Four friends, and what each of them wants")
            for traveler in state.travelers:
                preferences = traveler.preferences
                print(f"    {traveler.name:<6} {traveler.role}")
                print(
                    f"           flights: price {preferences.flight.price_importance:.1f}, "
                    f"nonstop {preferences.flight.nonstop_importance:.1f}"
                )
                print(
                    f"           hotels:  price {preferences.hotel.price_importance:.1f}, "
                    f"location {preferences.hotel.location_importance:.1f}"
                    + (
                        f", at least {preferences.hotel.min_rating:g}/5"
                        if preferences.hotel.min_rating
                        else ""
                    )
                )
                if preferences.food.dietary_restrictions:
                    print(f"           food:    {', '.join(preferences.food.dietary_restrictions)}")
                if preferences.activity.interests or preferences.activity.avoided:
                    print(
                        f"           wants:   {', '.join(preferences.activity.interests) or '-'}"
                        f"   avoids: {', '.join(preferences.activity.avoided) or '-'}"
                    )
                print(
                    f"           pace:    {preferences.pace.intensity}, "
                    f"{preferences.pace.max_daily_walking_km:g} km/day, "
                    f"starts {preferences.pace.preferred_start_time}"
                )

            # --- 2. The conflicts -------------------------------------------
            banner("2. Where they disagree")
            for conflict in detect_conflicts(state):
                print(f"    [{conflict.severity}] {conflict.kind}")
                print(f"       {conflict.summary}")
                for traveler_id, stance in sorted(conflict.positions.items()):
                    print(f"         {traveler_names(state)[traveler_id]:<5} {stance}")
                if conflict.resolution_options:
                    print(f"       ways out: {conflict.resolution_options[0]}")
                print()
            print(
                "    Every one of those keeps both sides. None of them is a single number\n"
                "    standing in for people who want different things."
            )

            # --- 3. Hotels, per person --------------------------------------
            banner("3. The same hotels, scored per person")
            options = fixture_hotels()
            source = "illustrative fixtures"

            if toolbox.hotels is not None:
                spec = SearchHotelsInput(
                    city="Tokyo",
                    area_name="Ueno",
                    check_in=CHECK_IN,
                    check_out=CHECK_OUT,
                    adults=4,
                    limit=12,
                )
                found = await toolbox.hotels.search_hotels(spec, state=state)
                if found.ok and found.results:
                    options = found.results[:4]
                    source = "live Google Hotels prices"
                else:
                    print("    live hotel search returned nothing; using fixtures\n")

            show_group(state, options, source)

            # --- 4. Cy's dinner, checked against live data ------------------
            banner("4. Cy is vegetarian. What does Google actually confirm?")
            entities = []
            for query in ("izakaya in Asakusa Tokyo", "Indian restaurant in Asakusa Tokyo"):
                found = await toolbox.places.search_places(
                    SearchPlacesInput(query=query, lat=0.0, lng=0.0, limit=4)
                )
                if not found.ok:
                    print(f"    place search failed: {found.error.message}")
                    return 1

                details = await toolbox.places.get_place_details(
                    GetPlaceDetailsInput(
                        place_ids=[place.place_id for place in found.results[:3]],
                        field_set=PlaceFieldSet.FULL,
                    )
                )
                if not details.ok:
                    print(f"    details failed: {details.error.message}")
                    return 1

                print(f"    {query}")
                resolved = resolve_places(details.results, {})
                for entity in resolved:
                    verdict = (
                        "CONFIRMED serves vegetarian food"
                        if entity.serves_vegetarian
                        else "unverified - Google has not said either way"
                    )
                    print(f"       {entity.name[:40]:<42} {verdict}")
                entities.extend(resolved)
                print()

            print(
                "    Google attests that a place does serve vegetarian food, and the second\n"
                "    group shows it doing so. It never attests the opposite, so an unverified\n"
                "    izakaya is unchecked rather than unsuitable - and is not removed."
            )

            # --- 5. What the trip may not claim -----------------------------
            state.entities = {entity.entity_id: entity for entity in entities}
            state.itinerary = TripItinerary(
                days=[
                    ItineraryDay(
                        date=CHECK_IN,
                        items=[
                            ItineraryItem(
                                item_id=f"item_{index}",
                                type="restaurant",
                                entity_id=entity.entity_id,
                                title=entity.name,
                            )
                            for index, entity in enumerate(entities[:3])
                        ],
                    )
                ]
            )

            banner("5. What this trip is not allowed to say yet")
            blocking = unresolved_blocking(state)
            if not blocking:
                print("    Nothing blocking. Every dinner is confirmed to suit Cy.")
                return 0

            for conflict in blocking:
                print(f"    {conflict.summary}\n")
                for option in conflict.resolution_options:
                    print(f"       - {option}")

            print(
                "\n    Planning carries on: search, research, generate and replan all still\n"
                "    work. The one thing refused is calling the trip ready, and the patch\n"
                "    engine enforces that rather than the prompt asking nicely."
            )
            return 0
    except MissingCredentials as exc:
        print(f"\n{exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
