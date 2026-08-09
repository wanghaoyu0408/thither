"""Getting to a place and stopping the car, as facts with sources.

The invariant this file is built around, and the reason it is not a boolean:

    **Missing parking data is not "no parking".**

Google not publishing a parking field for a beach means Google did not publish
a parking field. Treating that as "there is nowhere to park" would delete real
places from real trips on no evidence, which is the same failure as reading
`servesVegetarianFood: false` as a denial. So `availability` starts at
`unknown`, and only evidence moves it - in either direction.

`unavailable` is reserved for the case where something reliable actually says
so. It is the rarest value here and the only one that may block a plan.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import LatLng, new_id, utcnow

ParkingAvailability = Literal["confirmed", "likely", "unknown", "unavailable"]

ParkingKind = Literal[
    "onsite",
    "parking_lot",
    "parking_garage",
    "street",
    "valet",
    "other",
]

# What it costs to leave a car there. `unknown` is a real answer: a lot that
# publishes no price is not a free lot.
ParkingCost = Literal["free", "paid", "unknown"]

ArrivalMode = Literal["driving", "transit", "walking", "unknown"]


class ParkingSpot(BaseModel):
    """One place you could actually leave the car."""

    kind: ParkingKind = "other"
    cost: ParkingCost = "unknown"

    name: str | None = None
    # The resolved place, when this is somewhere the trip knows about.
    entity_id: str | None = None
    location: LatLng | None = None

    # Measured, never estimated. `None` means nobody measured it.
    walking_meters: int | None = None
    walking_minutes: float | None = None

    notes: str | None = None

    def describe(self) -> str:
        cost = {"free": "Free", "paid": "Paid", "unknown": "Cost unknown"}[self.cost]
        kind = self.kind.replace("_", " ")
        walk = (
            f" · {self.walking_minutes:.0f} min walk"
            if self.walking_minutes is not None
            else " · walk not measured"
        )
        return f"{cost} {kind}{walk}"


class ParkingContext(BaseModel):
    """What is known about parking at one place, and how firmly."""

    availability: ParkingAvailability = "unknown"

    spots: list[ParkingSpot] = []

    # Tri-state by the same rule as availability: `None` is "nobody said",
    # not "no permit needed".
    permit_required: bool | None = None
    reservation_required: bool | None = None

    notes: str | None = None
    # Evidence ids backing the claims above. An official rule - a permit, a
    # closure - needs one of these; community chatter may inform the notes but
    # may not set `permit_required`.
    source_refs: list[str] = []

    observed_at: datetime = Field(default_factory=utcnow)

    @property
    def is_known(self) -> bool:
        return self.availability in ("confirmed", "likely")

    @property
    def best(self) -> ParkingSpot | None:
        """The spot to plan around: the shortest measured walk, else the first.

        An unmeasured walk never wins, because "unknown" would otherwise sort
        as zero and pick the option nobody has checked.
        """
        measured = [spot for spot in self.spots if spot.walking_minutes is not None]
        if measured:
            return min(measured, key=lambda spot: spot.walking_minutes)
        return self.spots[0] if self.spots else None

    def headline(self) -> str:
        if self.availability == "unavailable":
            return "No parking here"
        if self.availability == "unknown":
            return "Parking needs verification"
        spot = self.best
        return spot.describe() if spot else "Parking available"


class ArrivalContext(BaseModel):
    """How you get to a place and what happens between the car and the door."""

    arrival_id: str = Field(default_factory=lambda: new_id("arr"))
    entity_id: str

    mode: ArrivalMode = "unknown"

    parking: ParkingContext = ParkingContext()

    # Where you actually stop - a car park entrance, a trailhead, a drop-off -
    # when it is somewhere other than the place's own centroid. Routing to a
    # beach's centroid can mean routing into the sea.
    access_point: LatLng | None = None
    access_point_name: str | None = None

    arrival_notes: str | None = None
    source_refs: list[str] = []

    observed_at: datetime = Field(default_factory=utcnow)

    @property
    def overhead_minutes(self) -> float | None:
        """Minutes between arriving and being at the door, where measured.

        Only the walk. Time spent circling for a space is real and nobody
        publishes it, so it is not invented here.
        """
        spot = self.parking.best
        return spot.walking_minutes if spot else None
