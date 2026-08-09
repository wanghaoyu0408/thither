from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.common import Money, TimeFlexibility, new_id
from app.models.weather import WeatherContext

ItemType = Literal[
    "flight",
    "hotel",
    "restaurant",
    "activity",
    "transport",
    "free_time",
]

ItemStatus = Literal["proposed", "selected", "locked"]


class DaySummary(BaseModel):
    """What a day costs in time, distance and money, as far as it is known.

    Every figure here is measured, never estimated to fill a gap - so most of
    them come in pairs with a count of what was measured. `legs_measured` out of
    `legs_total` is what turns "58 min driving" into "58 min driving, 3 of 4
    legs measured", and stops a day whose routes half failed from reading as a
    short day.

    A missing leg contributes nothing to a total and one to `legs_total`. It
    never contributes zero minutes, which would be a claim.
    """

    stops: int = 0

    walking_minutes: float | None = None
    driving_minutes: float | None = None
    total_walking_km: float | None = None
    total_transit_minutes: int | None = None

    # How much of the day's travel is actually behind those numbers.
    legs_total: int = 0
    legs_measured: int = 0

    estimated_cost: Money | None = None
    # Items that carried a price, out of the items on the day. Below 1.0 the
    # figure above is a partial and has to be shown as one.
    items_priced: int = 0
    items_total: int = 0

    pace: str | None = None
    locked_items: int = 0
    unresolved_issues: int = 0

    measured_at: datetime | None = None
    notes: str | None = None

    @property
    def travel_is_partial(self) -> bool:
        return self.legs_measured < self.legs_total

    @property
    def cost_is_partial(self) -> bool:
        return self.items_priced < self.items_total

    @property
    def cost_coverage(self) -> float:
        return self.items_priced / self.items_total if self.items_total else 0.0


class ItineraryItem(BaseModel):
    item_id: str = Field(default_factory=lambda: new_id("item"))

    type: ItemType

    # Points into TripState.entities. Null for free time / generic transport.
    entity_id: str | None = None

    title: str

    start_at: datetime | None = None
    end_at: datetime | None = None

    time_flexibility: TimeFlexibility = "flexible"

    status: ItemStatus = "proposed"

    # Total cost for the whole party, not per person.
    estimated_cost: Money | None = None

    reservation_required: bool | None = None
    reservation_booked: bool | None = None

    notes: str | None = None

    @model_validator(mode="after")
    def _check_time_order(self) -> "ItineraryItem":
        if self.start_at and self.end_at and self.end_at < self.start_at:
            raise ValueError(f"item {self.item_id}: end_at is before start_at")
        return self


class ItineraryDay(BaseModel):
    date: date

    theme: str | None = None

    # Neighborhoods/areas this day is built around, for geographic clustering.
    area_ids: list[str] = []

    items: list[ItineraryItem] = []

    summary: DaySummary | None = None

    # Stored rather than fetched at read time, so validating a trip never makes
    # a network call and an old forecast is visibly old rather than quietly
    # absent. `observed_at` on the context is what makes staleness legible.
    weather: WeatherContext | None = None


class TripItinerary(BaseModel):
    days: list[ItineraryDay] = []
    generated_at: datetime | None = None

    def iter_items(self):
        """(day, item) for every item across the trip."""
        for day in self.days:
            for item in day.items:
                yield day, item
