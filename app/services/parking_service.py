"""Where you leave the car, and how far it is from there to the door.

Ordered so the cheapest evidence is used first: what Google publishes about the
place itself, then a nearby search for lots and garages, then a real walking
measurement from the best of them to the entrance.

The rule everything here obeys:

    **A gap in the data leaves `availability` at `unknown`.**

Not `unavailable`. A beach with no published parking field is a beach nobody
has told us about, and marking it as having no parking would quietly delete it
from trips on no evidence. `unavailable` is reserved for something that
actually says so, and it is the only value here that may block a plan.

Community sources may fill `notes` - a local saying the lot fills by nine is
worth knowing - but may never set `permit_required` or `reservation_required`.
Those are official rules and want an official source.
"""

from app.models.arrival import ArrivalContext, ParkingContext, ParkingSpot
from app.models.common import LatLng
from app.models.entity import PlaceEntity
from app.models.place import PlaceFieldSet, SearchPlacesInput
from app.models.route import GetRoutesInput, LocationRef
from app.models.trip import TripState

# Google place types that are somewhere to leave a car.
PARKING_TYPES = ("parking",)

# How far away a car park stops being this place's car park.
SEARCH_RADIUS_M = 800
# More than this on foot and it is not really parking *for* the attraction.
USEFUL_WALK_MINUTES = 20.0
MAX_CANDIDATES = 4

# Google's own `parkingOptions` keys, and what each one means for us.
_ONSITE_KEYS = ("freeParkingLot", "paidParkingLot", "freeGarageParking", "paidGarageParking")


def _from_places(entity: PlaceEntity) -> list[ParkingSpot]:
    """What Google says about parking at the place itself.

    Only positives are read. Google omitting `freeParkingLot` is not Google
    saying there is no free lot, so an absent key adds nothing either way.
    """
    options = (entity.parking_options or {}) if hasattr(entity, "parking_options") else {}
    spots: list[ParkingSpot] = []
    for key in _ONSITE_KEYS:
        if not options.get(key):
            continue
        spots.append(
            ParkingSpot(
                kind="parking_garage" if "Garage" in key else "parking_lot",
                cost="free" if key.startswith("free") else "paid",
                name=f"On-site {'garage' if 'Garage' in key else 'car park'}",
                entity_id=entity.entity_id,
                location=LatLng(lat=entity.lat, lng=entity.lng),
                # On-site: the walk is from the car park to the door and Google
                # does not publish it. Left unmeasured rather than called zero.
                notes="published by Google as on-site parking",
            )
        )
    if options.get("freeStreetParking"):
        spots.append(
            ParkingSpot(
                kind="street",
                cost="free",
                name="Free street parking",
                entity_id=entity.entity_id,
                location=LatLng(lat=entity.lat, lng=entity.lng),
                notes="published by Google as free street parking nearby",
            )
        )
    if options.get("valetParking"):
        spots.append(
            ParkingSpot(
                kind="valet",
                cost="paid",
                name="Valet",
                entity_id=entity.entity_id,
                location=LatLng(lat=entity.lat, lng=entity.lng),
            )
        )
    return spots


class ParkingService:
    """Builds an `ArrivalContext` for one place."""

    def __init__(self, places, routes) -> None:
        self._places = places
        self._routes = routes

    async def context_for(
        self, state: TripState, entity: PlaceEntity, *, mode: str = "driving"
    ) -> ArrivalContext:
        onsite = _from_places(entity)
        spots = list(onsite)
        warnings: list[str] = []

        if not onsite:
            found, problems = await self._nearby(entity)
            spots.extend(found)
            warnings.extend(problems)

        spots = await self._measure_walks(entity, spots)

        # Anything with a measured walk longer than a reasonable one is not
        # parking for this place; keep it, but it does not make the place
        # "confirmed".
        useful = [
            spot
            for spot in spots
            if spot.walking_minutes is None or spot.walking_minutes <= USEFUL_WALK_MINUTES
        ]
        if spots and not useful:
            # "Nothing found" and "everything found was too far" are different
            # answers, and an unexplained `unknown` is the least useful thing
            # this can return.
            nearest = min(
                (s.walking_minutes for s in spots if s.walking_minutes is not None), default=None
            )
            warnings.append(
                f"the nearest car park found is {nearest:.0f} min on foot, beyond the "
                f"{USEFUL_WALK_MINUTES:.0f} min this counts as parking for a place"
                if nearest is not None
                else "car parks were found nearby but none could be measured"
            )

        if onsite:
            # Google asserting on-site parking is a positive assertion.
            availability = "confirmed"
        elif useful:
            # A car park nearby is good evidence that parking exists, and weaker
            # evidence than the place saying so itself.
            availability = "likely"
        else:
            availability = "unknown"

        return ArrivalContext(
            entity_id=entity.entity_id,
            mode=mode,  # type: ignore[arg-type]
            parking=ParkingContext(
                availability=availability,
                spots=useful,
                notes="; ".join(warnings) or None,
            ),
            access_point=(
                LatLng(lat=best.location.lat, lng=best.location.lng)
                if (best := next((s for s in useful if s.location), None))
                else None
            ),
            access_point_name=(best.name if best else None),
        )

    async def _nearby(self, entity: PlaceEntity) -> tuple[list[ParkingSpot], list[str]]:
        result = await self._places.search_places(
            SearchPlacesInput(
                categories=list(PARKING_TYPES),
                lat=entity.lat,
                lng=entity.lng,
                radius_meters=SEARCH_RADIUS_M,
                limit=MAX_CANDIDATES,
                field_set=PlaceFieldSet.BASIC,
            )
        )
        if not result.ok:
            # A failed lookup is not an empty car park. Say which it was.
            return [], [f"the nearby parking search failed: {result.error.message}"]
        if not result.results:
            return [], [f"no car park found within {SEARCH_RADIUS_M} m"]

        return [
            ParkingSpot(
                kind="parking_lot",
                cost="unknown",
                name=place.name,
                location=LatLng(lat=place.lat, lng=place.lng),
                notes="found nearby; Google does not publish whether it is free",
            )
            for place in result.results
        ], []

    async def _measure_walks(
        self, entity: PlaceEntity, spots: list[ParkingSpot]
    ) -> list[ParkingSpot]:
        """Walking time from each car park to the entrance, from the Routes API.

        The whole point of the feature. A car park twelve minutes away turns a
        10:00 arrival into a 10:12 one, and no amount of driving time says so.
        """
        targets = [spot for spot in spots if spot.location and not spot.entity_id]
        if not targets:
            return spots

        result = await self._routes.get_routes(
            GetRoutesInput(
                origins=[
                    LocationRef(lat=spot.location.lat, lng=spot.location.lng) for spot in targets
                ],
                destinations=[LocationRef(lat=entity.lat, lng=entity.lng)],
                mode="walking",
            )
        )
        if not result.ok:
            return spots

        for leg in result.results:
            if leg.status != "ok" or leg.duration_seconds is None:
                continue
            spot = targets[leg.origin_index]
            spot.walking_minutes = round(leg.duration_seconds / 60.0, 1)
            if leg.distance_meters is not None:
                spot.walking_meters = int(leg.distance_meters)
        return spots
