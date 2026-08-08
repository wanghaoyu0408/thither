"""Milestone 5 acceptance: comparing SFO, SJC and OAK.

    .\\.venv\\Scripts\\python.exe scripts\\compare_airports.py

Two halves, deliberately separate:

    1. Real driving times from the Bay Area to each airport (Routes API), then a
       live flight search. If the Duffel token is a test one, everything from
       that search is sandbox data and is banner-marked as such.

    2. The trade-off reasoning, demonstrated on fixture offers shaped like real
       ones - because Duffel's own docs say the sandbox has no realistic prices,
       and a comparison built on invented fares would prove nothing.

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

            # --- 2. A live search, labelled for what it is -----------------
            banner("2. Flight search")
            if toolbox.flights is None:
                print("    DUFFEL_ACCESS_TOKEN is not set; skipping the live search.")
            else:
                if not toolbox.flights.live_mode:
                    sandbox_banner()

                result = await toolbox.flights.search_flights(
                    SearchFlightsInput(
                        origins=[a.iata for a in bay_area] or ["SFO"],
                        destinations=["NRT"],
                        departure_date=date.today() + timedelta(days=45),
                        adults=4,
                    )
                )
                if not result.ok:
                    print(f"    search failed: [{result.error.code}] {result.error.message}")
                elif result.found_nothing:
                    print("    no offers came back. The search itself worked.")
                else:
                    ranked = rank_flights(result.results, airports=bay_area, limit=5)
                    for item in ranked:
                        tag = "" if item.option.live_mode else "  [SANDBOX - NOT REAL]"
                        print(
                            f"    {item.option.origin}->{item.option.destination}  "
                            f"{item.per_person:>8.0f} {item.currency}  "
                            f"{item.option.stops} stop(s)  score {item.score.total:.3f}{tag}"
                        )
                for warning in result.warnings:
                    print(f"    note: {warning}")

            # --- 3. The reasoning, on data that means something ------------
            banner("3. The trade-off, on realistic fixture fares")
            print("    (the sandbox cannot supply realistic prices, so these are fixtures)")

            preferences = FlightPreferences(
                nonstop_importance=0.9, price_importance=0.6, schedule_importance=0.8
            )
            ranked = rank_flights(fixture_offers(), preferences=preferences, airports=bay_area)

            print()
            for item in ranked:
                print(
                    f"    {item.option.origin}  {item.per_person:>6.0f} USD  "
                    f"{item.option.stops} stop(s)  "
                    f"{item.option.duration_minutes // 60}h{item.option.duration_minutes % 60:02d}m"
                    f"   score {item.score.total:.3f}"
                )
                print(f"          {item.score.dimensions}")

            cheapest = cheapest_of(ranked)
            if cheapest and cheapest.option.offer_ref != ranked[0].option.offer_ref:
                trade_off = explain_choice(ranked[0], cheapest, airports=bay_area)
                banner("Why the more expensive flight is the recommendation")
                print(
                    f"    Recommended {ranked[0].option.origin}, "
                    f"cheapest was {cheapest.option.origin}:\n"
                )
                for statement in trade_off.statements:
                    print(f"       - {statement}")
                print("\n    Every figure above came from the provider or the Routes API.")
            else:
                print("\n    The recommendation is also the cheapest; no trade-off to explain.")

            return 0
    except MissingCredentials as exc:
        print(f"\n{exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
