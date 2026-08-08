"""Finding the airports worth comparing (spec section 14).

The interesting part is not the lookup - it is the last step. "SFO or SJC?" is
only answerable with a real driving time, and spec section 21 forbids estimating
one when the Routes API exists. So the straight-line distance here is a filter
and never a claim; what reaches the user comes from M2's route service.

The dataset is OurAirports, public domain, vendored whole. Queries narrow it to
airports with an IATA code and scheduled service, which is a decision the caller
makes rather than one baked into the file.
"""

import csv
import gzip
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.models.flight import AirportOption, SearchAirportsInput
from app.models.place import SearchPlacesInput
from app.models.route import GetRoutesInput, LocationRef
from app.models.tool import ToolError, ToolResult
from app.services.geo import haversine_km
from app.services.place_service import PlaceService
from app.services.route_service import RouteService

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "airports.csv.gz"

# One matrix call covers this many airports; more than a handful is not a
# comparison a traveller can act on anyway.
MAX_GROUND_LOOKUPS = 8


@dataclass(frozen=True)
class AirportRecord:
    ident: str
    kind: str
    name: str
    lat: float
    lng: float
    country: str
    city: str
    iata: str
    scheduled: bool

    @property
    def serves_passengers(self) -> bool:
        return bool(self.iata) and self.scheduled


@lru_cache(maxsize=1)
def load_airports() -> list[AirportRecord]:
    """The whole dataset, parsed once."""
    if not DATA_FILE.exists():
        return []

    records: list[AirportRecord] = []
    with gzip.open(DATA_FILE, "rt", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                lat = float(row["latitude_deg"])
                lng = float(row["longitude_deg"])
            except (TypeError, ValueError):
                continue
            records.append(
                AirportRecord(
                    ident=row["ident"],
                    kind=row["type"],
                    name=row["name"],
                    lat=lat,
                    lng=lng,
                    country=row["iso_country"],
                    city=row["municipality"],
                    iata=row["iata_code"].strip().upper(),
                    scheduled=row["scheduled_service"] == "yes",
                )
            )
    return records


@lru_cache(maxsize=1)
def _by_iata() -> dict[str, AirportRecord]:
    return {record.iata: record for record in load_airports() if record.iata}


def find_by_iata(code: str) -> AirportRecord | None:
    return _by_iata().get(code.strip().upper())


SIZE_RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}


def nearby(
    lat: float,
    lng: float,
    *,
    radius_km: float,
    limit: int,
    include_secondary: bool = False,
) -> list[AirportRecord]:
    """Passenger airports within a straight-line radius, nearest first.

    The dataset's `scheduled_service` flag cannot be trusted on its own: it is
    set on Buchanan Field and San Carlos, which no airline serves, and they
    turned up beside SFO. Nor does `type` separate them - both are recorded as
    medium, the same as genuinely commercial fields like Burbank.

    So the rule is comparative rather than absolute: **major airports when there
    are any, secondary ones only when there are not.** A Bay Area search gets
    SFO, OAK and SJC; a region whose only airport is medium still gets it.
    """
    scored = [
        (haversine_km(lat, lng, record.lat, record.lng), record)
        for record in load_airports()
        if record.serves_passengers
    ]
    within = [(distance, record) for distance, record in scored if distance <= radius_km]

    if not include_secondary:
        major = [row for row in within if row[1].kind == "large_airport"]
        if major:
            within = major

    within.sort(key=lambda row: (round(row[0]), SIZE_RANK.get(row[1].kind, 3), row[1].iata))
    return [record for _distance, record in within[:limit]]


class AirportService:
    def __init__(self, places: PlaceService, routes: RouteService) -> None:
        self._places = places
        self._routes = routes

    async def search_airports(self, spec: SearchAirportsInput) -> ToolResult[AirportOption]:
        origin = await self._geocode(spec.location)
        if origin is None:
            return ToolResult[AirportOption](
                source="ourairports",
                error=ToolError(
                    code="invalid_request",
                    message=f"could not locate {spec.location!r}",
                    provider="google_places",
                ),
            )

        warnings: list[str] = []
        records = nearby(
            *origin,
            radius_km=spec.radius_km,
            limit=spec.limit,
            include_secondary=spec.include_secondary_airports,
        )
        if not records:
            return ToolResult[AirportOption](
                source="ourairports",
                warnings=[f"no scheduled-service airport within {spec.radius_km:.0f} km"],
            )

        options = [
            AirportOption(
                iata=record.iata,
                icao=record.ident,
                name=record.name,
                city=record.city or None,
                country=record.country or None,
                lat=record.lat,
                lng=record.lng,
                distance_km=round(haversine_km(*origin, record.lat, record.lng), 1),
            )
            for record in records
        ]

        if spec.with_ground_travel:
            warnings.extend(await self._add_ground_travel(origin, options))

        if spec.max_ground_travel_minutes is not None:
            kept = [
                option
                for option in options
                if option.ground_travel_minutes is None
                or option.ground_travel_minutes <= spec.max_ground_travel_minutes
            ]
            dropped = len(options) - len(kept)
            if dropped:
                warnings.append(
                    f"{dropped} airport(s) beyond {spec.max_ground_travel_minutes} minutes' drive"
                )
            # An unknown drive time is kept rather than silently filtered - the
            # caller can see it is unknown and decide.
            options = kept

        options.sort(
            key=lambda option: (
                option.ground_travel_minutes
                if option.ground_travel_minutes is not None
                else float("inf"),
                option.distance_km or float("inf"),
                option.iata,
            )
        )
        return ToolResult[AirportOption](source="ourairports", results=options, warnings=warnings)

    async def _geocode(self, location: str) -> tuple[float, float] | None:
        found = await self._places.search_places(
            SearchPlacesInput(query=location, lat=0.0, lng=0.0, limit=1)
        )
        if not found.ok or found.found_nothing:
            return None
        first = found.results[0]
        if first.lat is None or first.lng is None:
            return None
        return first.lat, first.lng

    async def _add_ground_travel(
        self, origin: tuple[float, float], options: list[AirportOption]
    ) -> list[str]:
        """One driving matrix for every candidate at once."""
        targets = options[:MAX_GROUND_LOOKUPS]
        if not targets:
            return []

        result = await self._routes.get_routes(
            GetRoutesInput(
                origins=[LocationRef(lat=origin[0], lng=origin[1], label="origin")],
                destinations=[
                    LocationRef(lat=option.lat, lng=option.lng, label=option.iata)
                    for option in targets
                ],
                mode="driving",
            )
        )

        if not result.ok:
            for option in targets:
                option.ground_travel_source = "unavailable"
            return [f"ground travel times unavailable: {result.error.message}"]

        for leg in result.results:
            if leg.destination_index >= len(targets):
                continue
            option = targets[leg.destination_index]
            if leg.status == "ok" and leg.duration_minutes is not None:
                option.ground_travel_minutes = round(leg.duration_minutes, 1)
                option.ground_travel_source = "routes_api"
            else:
                option.ground_travel_source = "unavailable"

        return list(result.warnings)
