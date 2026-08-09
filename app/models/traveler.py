from datetime import datetime

from pydantic import BaseModel, Field

from app.models.common import Cabin, ClockTime, Importance, Pace, new_id, utcnow


class FlightPreferences(BaseModel):
    nonstop_importance: Importance = 0.5
    price_importance: Importance = 0.5
    schedule_importance: Importance = 0.5
    avoid_red_eye: bool = False
    preferred_airlines: list[str] = []
    avoided_airlines: list[str] = []
    cabin: Cabin = "economy"


class HotelPreferences(BaseModel):
    location_importance: Importance = 0.5
    price_importance: Importance = 0.5
    room_size_importance: Importance = 0.5
    quiet_importance: Importance = 0.5
    min_rating: float | None = None


class FoodPreferences(BaseModel):
    cuisines: list[str] = []
    dietary_restrictions: list[str] = []
    queue_tolerance: Importance = 0.5
    fine_dining_interest: Importance = 0.5
    adventurousness: Importance = 0.5


class ActivityPreferences(BaseModel):
    interests: list[str] = []
    avoided: list[str] = []
    museum_interest: Importance = 0.5
    nature_interest: Importance = 0.5
    nightlife_interest: Importance = 0.5
    shopping_interest: Importance = 0.5


class PacePreferences(BaseModel):
    preferred_start_time: ClockTime = "09:00"
    max_daily_walking_km: float = 12.0
    intensity: Pace = "balanced"


class TravelerProfile(BaseModel):
    """Long-term preferences for one person, reused across trips.

    Only updated from explicit user statements or confirmed strong evidence -
    never from weak inference (spec section 4).
    """

    profile_id: str = Field(default_factory=lambda: new_id("user"))
    # Bumped on every stored update. A trip snapshots preferences and has to be
    # able to name which version it took them from; `updated_at` is a timestamp,
    # not an identity, and two edits in the same second would be indistinguishable.
    revision: int = 0

    name: str

    home_city: str | None = None
    preferred_airports: list[str] = []

    flight_preferences: FlightPreferences = FlightPreferences()
    hotel_preferences: HotelPreferences = HotelPreferences()
    food_preferences: FoodPreferences = FoodPreferences()
    activity_preferences: ActivityPreferences = ActivityPreferences()
    pace_preferences: PacePreferences = PacePreferences()

    notes: str | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
