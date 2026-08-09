"""Milestone 6 acceptance: the neighbourhood first, then the hotels.

    .\\.venv\\Scripts\\python.exe scripts\\choose_hotel_area.py

Four parts:

    1. The trip's anchor places - what an area's convenience is measured against.
    2. Candidate neighbourhoods ranked by REAL travel time to each anchor.
    3. The recommendation, in figures that all came from the Routes API.
    4. Hotels inside the chosen area - only once an area exists.

Part four needs SERPAPI_API_KEY. Without it the script says so and stops after
part three, which is still the first and more important half of spec section 25
demonstrated end to end on live data.

Needs GOOGLE_MAPS_API_KEY.
"""

import asyncio
import sys
from datetime import date, timedelta

from app.config import get_settings
from app.models.entity import PlaceEntity
from app.models.hotel import SearchHotelsInput
from app.models.trip import DestinationSpec, TripBrief, TripDates, TripDecisions, TripState
from app.services.hotel_area_service import build_area_decision
from app.services.hotel_ranking import describe_prices, describe_ratings, explain_hotel_choice
from app.services.toolbox import MissingCredentials, Toolbox

# Japanese hotel and vendor names come back from the providers, and Windows
# pipes stdout as cp1252, which cannot encode them. Without this the script dies
# on its own output the moment it is redirected anywhere.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BANNER = "!" * 78

CHECK_IN = date.today() + timedelta(days=45)
CHECK_OUT = CHECK_IN + timedelta(days=4)

# A real east-Tokyo trip. Everything on it is out by the river, which is what
# should make Asakusa win and Shinjuku lose.
ANCHORS = [
    ("Senso-ji", 35.7148, 139.7967),
    ("Tokyo Skytree", 35.7101, 139.8107),
    ("Ueno Park", 35.7148, 139.7737),
    ("Akihabara Radio Kaikan", 35.6984, 139.7731),
    ("teamLab Planets", 35.6486, 139.7906),
]

CANDIDATE_AREAS = ["Asakusa", "Ueno", "Ginza", "Shinjuku", "Shibuya"]


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'-' * 78}")


def sandbox_banner() -> None:
    print(f"\n{BANNER}")
    print("!!  SANDBOX DATA - these hotels and prices are NOT REAL.")
    print("!!  Nothing below should be shown to a traveller as a real hotel.")
    print(f"{BANNER}\n")


def tokyo_trip() -> TripState:
    state = TripState.new(title="Tokyo, east side")
    state.brief = TripBrief(
        destination=DestinationSpec(city="Tokyo", country="Japan", flexible=False),
        timezone="Asia/Tokyo",
        dates=TripDates(start=CHECK_IN, end=CHECK_OUT),
    )
    state.entities = {
        f"ent_{index}": PlaceEntity(
            entity_id=f"ent_{index}",
            name=name,
            lat=lat,
            lng=lng,
            categories=["tourist_attraction"],
        )
        for index, (name, lat, lng) in enumerate(ANCHORS)
    }
    return state


async def main() -> int:
    settings = get_settings()
    if not settings.google_maps_api_key:
        print("\nMissing GOOGLE_MAPS_API_KEY in .env\n")
        return 1

    state = tokyo_trip()

    try:
        async with Toolbox(settings) as toolbox:
            # --- 1. What the area is being measured against ----------------
            banner("1. The trip's anchor places")
            for entity in state.entities.values():
                print(f"    {entity.name:<28} {entity.lat:.4f}, {entity.lng:.4f}")
            print(
                "\n    An area is only convenient relative to somewhere. These are the\n"
                "    somewhere."
            )

            # --- 2. Areas ranked on real travel time -----------------------
            banner(f"2. Neighbourhoods ranked by real travel time  ({', '.join(CANDIDATE_AREAS)})")
            areas = await toolbox.hotel_areas.recommend_areas(
                state, suggested_areas=CANDIDATE_AREAS, limit=6
            )
            if not areas.ok:
                print(f"    failed: [{areas.error.code}] {areas.error.message}")
                return 1
            if areas.found_nothing:
                print("    no candidate area could be located.")
                return 1

            # The mode actually measured, which is not always the one asked for:
            # Google has no transit data in Japan, so a Tokyo trip is compared
            # by car. Naming it here keeps the table honest.
            mode_used = areas.results[0].mode
            print(f"    times are {mode_used} minutes\n")
            print(f"    {'area':<26}{'mean':>7}{'worst':>8}{'reach':>8}{'score':>8}")
            for area in areas.results:
                mean = f"{area.mean_minutes:.0f}m" if area.mean_minutes is not None else "  -"
                worst = f"{area.worst_minutes:.0f}m" if area.worst_minutes is not None else "  -"
                print(
                    f"    {area.candidate.area_name:<26}{mean:>7}{worst:>8}"
                    f"{area.reachable:>4}/{area.anchor_count:<3}{area.score.total:>8.3f}"
                )
            for warning in areas.warnings:
                print(f"    note: {warning}")

            # --- 3. Why ----------------------------------------------------
            best = areas.results[0]
            banner(f"3. Recommended: {best.candidate.area_name}")
            print(f"    Minutes ({best.mode}) to each place this trip actually visits:")
            for entity_id, minutes in sorted(
                best.minutes_by_anchor.items(), key=lambda pair: pair[1]
            ):
                print(f"       {state.entities[entity_id].name:<28} {minutes:>5.0f} min")
            for entity_id in best.unreachable_anchors:
                print(f"       {state.entities[entity_id].name:<28}   no route found")

            print()
            for pro in best.pros:
                print(f"       + {pro}")
            for con in best.cons:
                print(f"       - {con}")
            print(f"\n    Dimensions: {best.score.dimensions}")
            if best.score.notes:
                print(f"    {best.score.notes}")

            if len(areas.results) > 1:
                runner_up = areas.results[1]
                if best.mean_minutes is not None and runner_up.mean_minutes is not None:
                    gap = runner_up.mean_minutes - best.mean_minutes
                    if gap >= 1.0:
                        print(
                            f"\n    Against {runner_up.candidate.area_name}: "
                            f"{gap:.0f} min less travel on average, every day of the trip."
                        )
                    else:
                        # Saying "0 min better" would be worse than saying nothing.
                        # What actually separated them belongs on the screen instead.
                        print(
                            f"\n    {runner_up.candidate.area_name} is within a minute of it on "
                            f"average ({runner_up.mean_minutes:.1f} against "
                            f"{best.mean_minutes:.1f}). What separated them:"
                        )
                        for name, value in best.score.dimensions.items():
                            other = runner_up.score.dimensions.get(name)
                            if other is not None and abs(value - other) >= 0.01:
                                print(f"       {name}: {value:.3f} against {other:.3f}")

            print(f"\n    Every minute above is a {best.mode} time from the Routes API.")

            # The decision the spec asks to be stored separately.
            state.decisions = TripDecisions(
                hotel_area=build_area_decision(areas.results, select_best=True)
            )

            # --- 4. Only now, hotels ---------------------------------------
            banner(f"4. Hotels in {best.candidate.area_name}")
            if toolbox.hotels is None:
                print(
                    "    SERPAPI_API_KEY is not set, so hotel prices cannot be searched.\n"
                    "    The area decision above is complete and live; this is the half\n"
                    "    that needs a hotel provider."
                )
                return 0

            if not toolbox.hotels.live_mode:
                sandbox_banner()

            spec = SearchHotelsInput(
                check_in=CHECK_IN,
                check_out=CHECK_OUT,
                adults=2,
                limit=20,
            )
            search = await toolbox.hotels.search_hotels(spec, state=state)
            if not search.ok:
                print(f"    search failed: [{search.error.code}] {search.error.message}")
                return 1
            if search.found_nothing:
                print("    no hotels came back. The search itself worked.")
                return 0

            shortlist = await toolbox.hotels.shortlist(
                search.results, state=state, spec=spec, size=5
            )

            for item in shortlist.ranked:
                price = f"{item.nightly:.0f} {item.currency}" if item.nightly else "no price"
                minutes = item.option.mean_route_minutes()
                travel = f"{minutes:.0f} min avg" if minutes is not None else "not measured"
                tag = "" if item.option.live_mode else "  [SANDBOX - NOT REAL]"
                print(f"\n    {item.option.name}{tag}")
                print(f"       {price}/night   {travel}   score {item.score.total:.3f}")
                # Two lists. A star category and a guest score are never merged.
                for described in describe_ratings(item.option):
                    print(f"       rating: {described}")
                for described in describe_prices(item.option)[:3]:
                    print(f"       price:  {described}")
                for pro in item.pros[:3]:
                    print(f"       + {pro}")
                for con in item.cons[:2]:
                    print(f"       - {con}")

            for warning in [*search.warnings, *shortlist.warnings]:
                print(f"\n    note: {warning}")

            if len(shortlist.ranked) > 1:
                first, second = shortlist.ranked[0], shortlist.ranked[1]
                trade_off = explain_hotel_choice(first, second)

                if trade_off.close_call:
                    # Manufacturing a winner out of a one-dollar gap would be
                    # exactly the confident wrongness this project avoids.
                    banner("Too close to call")
                    print(
                        f"    Nothing measured meaningfully separates "
                        f"{first.option.name} from {second.option.name}:\n"
                    )
                else:
                    banner("Why the recommendation wins")
                    print(f"    {first.option.name} over {second.option.name}:\n")

                for statement in trade_off.statements:
                    print(f"       - {statement}")
                if trade_off.close_call:
                    print("\n    Pick on whatever the tools cannot see: the photos, the street.")

            return 0
    except MissingCredentials as exc:
        print(f"\n{exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
