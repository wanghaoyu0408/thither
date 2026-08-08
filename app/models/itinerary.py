from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.common import Money, TimeFlexibility, new_id

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
    total_walking_km: float | None = None
    total_transit_minutes: int | None = None
    estimated_cost: Money | None = None
    notes: str | None = None


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


class TripItinerary(BaseModel):
    days: list[ItineraryDay] = []
    generated_at: datetime | None = None

    def iter_items(self):
        """(day, item) for every item across the trip."""
        for day in self.days:
            for item in day.items:
                yield day, item
