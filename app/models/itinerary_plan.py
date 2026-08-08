from datetime import date

from pydantic import BaseModel, Field

from app.models.common import ClockTime, Pace, new_id
from app.models.patch import PatchOperation, PatchScope
from app.models.validation import ValidationState


class PlanParams(BaseModel):
    """What the agent decides; the builder does the arithmetic."""

    days: int | None = None
    # Neighbourhood hints, e.g. ["Shibuya", "Asakusa"]. Semantic, so the model
    # supplies them; everything downstream is deterministic.
    areas: list[str] = []
    intensity: Pace | None = None
    day_start: ClockTime = "10:00"
    include_meals: bool = True


class ReplanParams(BaseModel):
    """A local replan of one day (spec section 29)."""

    intensity: Pace | None = None
    max_items: int | None = None
    # Kept in addition to anything locked.
    keep_item_ids: list[str] = []
    drop_item_ids: list[str] = []
    note: str | None = None


class PlannedItem(BaseModel):
    """Compact view of one scheduled item, for the model to read and explain."""

    item_id: str
    title: str
    type: str
    start_at: str | None = None
    end_at: str | None = None
    entity_id: str | None = None
    locked: bool = False
    opening_hours: str | None = None


class PlannedDay(BaseModel):
    date: date
    theme: str | None = None
    items: list[PlannedItem] = []


class ItineraryProposal(BaseModel):
    """A proposed change, not an applied one.

    Carrying an id means `apply_trip_patch` can take `proposal_id` rather than
    the whole operation list - the model never re-serializes the itinerary,
    which is cheaper and removes a place for it to corrupt the state.
    """

    proposal_id: str = Field(default_factory=lambda: new_id("prop"))

    trip_id: str
    base_revision: int

    scope: PatchScope | None = None
    operations: list[PatchOperation] = []

    days: list[PlannedDay] = []
    days_changed: list[date] = []

    summary: str = ""
    validation: ValidationState = ValidationState()
    warnings: list[str] = []

    @property
    def is_empty(self) -> bool:
        return not self.operations
