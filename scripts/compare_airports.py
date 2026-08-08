"""Milestone 5 acceptance: comparing SFO, SJC and OAK.

    .\\.venv\\Scripts\\python.exe scripts\\compare_airports.py

Three parts:

    1. Real driving times from the Bay Area to each airport (Routes API).
    2. A flight search from all three, shown both as an overall ranking and as
       the best option from each airport - the second is the comparison being
       asked for, since one airport usually takes every slot in the first.
    3. Why the recommendation wins, in figures that all came from a tool.

If the Duffel token is a test one, everything from the search is sandbox data
and is banner-marked as such; Duffel's own docs say test mode has no realistic
prices. Fixtures stand in for part three only when the search returns nothing
to compare against, and say so.

Needs GOOGLE_MAPS_API_KEY. DUFFEL_ACCESS_TOKEN is optional; without it part one
does the airports only.
"""

import asyncio
import sys
from datetime import date, datetime, timedelta

from app.config import get_settings
from app.models.common import Money
from app.models.decision import FlightOptionData
from app.models.flight import (
    AirportOption,
    FlightSegment,
    FlightSlice,
    SearchAirportsInput,
    SearchFlightsInput,
)
from app.models.traveler import FlightPreferences
from app.services.flight_ranking import cheapest_of, explain_choice, rank_flights
from app.services.toolbox import MissingCredentials, Toolbox

BANNER = "!" * 78


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'-' * 78}")


def sandbox_banner() -> None:
    print(f"\n{BANNER}")
    print("!!  SANDBOX DATA - these flights, times and prices are NOT REAL.")
    print("!!  The provider token is a test one. Nothing below is bookable,")
    print("!!  and none of it should be shown to a traveller as a real fare.")
    print(f"{BANNER}\n")


def show_ranked(ranked: list, *, dimensions: bool = False) -> None:
    for item in ranked:
        minutes = item.option.duration_minutes or 0
        tag = "" if item.option.live_mode else "  [SANDBOX - NOT REAL]"
        print(
            f"    {item.option.origin}->{item.option.destination}  "
            f"{item.per_person:>7.0f} {item.currency}  "
            f"{item.option.stops} stop(s)  {minutes // 60}h{minutes % 60:02d}m  "
            f"{','.join(item.option.airlines):<8} score {item.score.total:.3f}{tag}"
        )
        if dimensions:
            print(f"          {item.score.dimensions}")


def show_by_origin(ranked: list, airports: list[AirportOption]) -> None:
    """Best option from each airport, which is the comparison being asked for.

    A flat top-six hides it: when one airport dominates it takes every slot, and
    "should we drive to Oakland instead?" goes unanswered.
    """
    drive = {a.iata: a.ground_travel_minutes for a in airports}
    best: dict[str, object] = {}
    counts: dict[str, int] = {}
    for item in ranked:
        origin = item.option.origin
        counts[origin] = counts.get(origin, 0) + 1
        if origin not in best:
            best[origin] = item

    print("\n    Best from each airport:")
    for origin in sorted(best, key=lambda o: -best[o].score.total):
        item = best[origin]
        minutes = item.option.duration_minutes or 0
        drive_note = (
            f"{drive[origin]:.0f} min drive" if drive.get(origin) is not None else "drive unknown"
        )
        print(
            f"       {origin}  {item.per_person:>7.0f} {item.currency}  "
            f"{item.option.stops} stop(s)  {minutes // 60}h{minutes % 60:02d}m  "
            f"{drive_note:<16} {counts[origin]:>3} option(s)  score {item.score.total:.3f}"
        )


def pick_alternative(ranked: list):
    """What the recommendation should be compared against.

    Normally the cheapest. But when the best option is also the cheapest -
    which happens often on a route one airport dominates - the comparison worth
    showing is the best from a *different* airport, since "should we drive to
    Oakland instead?" is the question being asked.
    """
    if len(ranked) < 2:
        return None

    best = ranked[0]
    cheapest = cheapest_of(ranked)
    if cheapest is not None and cheapest.option.offer_ref != best.option.offer_ref:
        return cheapest

    return next((item for item in ranked[1:] if item.option.origin != best.option.origin), None)


def fixture_offers() -> list[FlightOptionData]:
    """Offers shaped like real ones, for the part the sandbox cannot demonstrate."""

    def build(ref, origin, price, stops, minutes, via=None):
        depart = datetime(2026, 10, 3, 11, 0)
        arrive = depart + timedelta(minutes=minutes)
        if stops == 0:
            segments = [
                FlightSegment(
                    origin=origin,
                    destination="NRT",
                    departing_at=depart,
                    arriving_at=arrive,
                    marketing_carrier="UA",
                    duration_minutes=minutes,
                )
            ]
        else:
            mid = depart + timedelta(minutes=minutes // 2)
            segments = [
                FlightSegment(
                    origin=origin,
                    destination=via,
                    departing_at=depart,
                    arriving_at=mid,
                    marketing_carrier="UA",
                ),
                FlightSegment(
                    origin=via,
                    destination="NRT",
                    departing_at=mid + timedelta(minutes=70),
                    arriving_at=arrive,
                    marketing_carrier="UA",
                ),
            ]
        return FlightOptionData(
            provider="fixture",
            offer_ref=ref,
            live_mode=True,
            price=Money(amount=price * 4, currency="USD"),
            price_per_person=Money(amount=price, currency="USD"),
            origin=origin,
            destination="NRT",
            slices=[
                FlightSlice(
                    origin=origin,
                    destination="NRT",
                    departing_at=depart,
                    arriving_at=arrive,
                    duration_minutes=minutes,
                    segments=segments,
                )
            ],
            departure_at=depart,
            arrival_at=arrive,
            duration_minutes=minutes,
            stops=stops,
            airlines=["UA"],
        )

    return [
        build("fx_sfo", "SFO", 642.0, 0, 655),
        build("fx_oak", "OAK", 600.0, 1, 890, via="LAX"),
        build("fx_sjc", "SJC", 618.0, 1, 920, via="SEA"),
    ]


async def main() -> int:
    settings = get_settings()
    if not settings.google_maps_api_key:
        print("\nMissing GOOGLE_MAPS_API_KEY in .env\n")
        return 1

    try:
        async with Toolbox(settings) as toolbox:
            # --- 1. Which airport is actually convenient -------------------
            banner("1. Real driving times from the Bay Area  (Google Routes)")
            found = await toolbox.airports.search_airports(
                SearchAirportsInput(location="San Francisco Bay Area", limit=5)
            )
            if not found.ok:
                print(f"    failed: {found.error.message}")
                return 1

            airports: list[AirportOption] = found.results
            for airport in airports:
                drive = (
                    f"{airport.ground_travel_minutes:>5.0f} min"
                    if airport.ground_travel_minutes is not None
                    else "  not available"
                )
                print(
                    f"    {airport.iata}  {drive}   {airport.distance_km:>6.1f} km  {airport.name}"
                )
            for warning in found.warnings:
                print(f"    note: {warning}")

            bay_area = [a for a in airports if a.iata in ("SFO", "OAK", "SJC")]

            preferences = FlightPreferences(
                nonstop_importance=0.9, price_importance=0.6, schedule_importance=0.8
            )

            # --- 2. A real search, labelled for what it is -----------------
            banner("2. Flight search: Bay Area -> Tokyo")
            ranked: list = []

            if toolbox.flights is None:
                print("    DUFFEL_ACCESS_TOKEN is not set; skipping the search.")
            else:
                if not toolbox.flights.live_mode:
                    sandbox_banner()

                result = await toolbox.flights.search_flights(
                    SearchFlightsInput(
                        origins=[a.iata for a in bay_area] or ["SFO"],
                        destinations=["NRT"],
                        departure_date=date.today() + timedelta(days=45),
                        adults=4,
                        limit=100,
                    )
                )
                if not result.ok:
                    print(f"    search failed: [{result.error.code}] {result.error.message}")
                elif result.found_nothing:
                    print("    no offers came back. The search itself worked.")
                else:
                    ranked = rank_flights(
                        result.results, preferences=preferences, airports=bay_area
                    )
                    show_ranked(ranked[:6])
                    show_by_origin(ranked, bay_area)
                for warning in result.warnings:
                    print(f"    note: {warning}")

            # --- 3. The trade-off -----------------------------------------
            alternative = pick_alternative(ranked)
            source = "real fares"

            if alternative is None:
                source = "illustrative fixtures"
                banner("3. The trade-off, on illustrative fixtures")
                print(
                    "    The search returned nothing to compare against. These fixtures show\n"
                    "    the reasoning that applies when it does."
                )
                ranked = rank_flights(fixture_offers(), preferences=preferences, airports=bay_area)
                alternative = pick_alternative(ranked)
                print()
                show_ranked(ranked, dimensions=True)
            else:
                banner("3. The trade-off, on the real fares above")

            if ranked and alternative is not None:
                best = ranked[0]
                trade_off = explain_choice(best, alternative, airports=bay_area)
                banner(f"Why the recommendation wins  ({source})")
                print(
                    f"    Recommended {best.option.origin} at "
                    f"{best.per_person:.0f} {best.currency}; "
                    f"compared against {alternative.option.origin} at "
                    f"{alternative.per_person:.0f} {alternative.currency}\n"
                )
                for statement in trade_off.statements:
                    print(f"       - {statement}")
                print("\n    Every figure above came from the provider or the Routes API.")
            elif ranked:
                print("\n    Only one option came back; there is nothing to compare it against.")

            return 0
    except MissingCredentials as exc:
        print(f"\n{exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
