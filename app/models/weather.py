"""Weather as a feasibility input, with the kind of claim it is always attached.

The distinction this file exists to keep is between a **forecast** and a
**historical norm**. A forecast is about a date: it can say it will probably
rain on Tuesday, and planning may act on that. A norm is about a season: it can
say August afternoons here are often wet, and planning may prepare for that -
but it can never say what Tuesday will do, because nobody knows.

Collapsing the two would produce the most confident kind of wrong answer this
project can make: a date-specific claim built from an average.
"""

from datetime import date as date_type
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import utcnow

WeatherKind = Literal["forecast", "historical_norm", "unavailable"]


class ClimatologyMethod(BaseModel):
    """How a norm was computed, so the number can be argued with.

    Persisted in full. "Typical high 29C" is not a fact on its own - it depends
    entirely on which years, which days around the date, and whose observations.
    """

    provider: str
    dataset: str

    sample_year_start: int
    sample_year_end: int
    # Days either side of the calendar date that were pooled in.
    calendar_window_days: int
    # Observations actually used, after gaps.
    sample_count: int

    computed_at: datetime = Field(default_factory=utcnow)

    def describe(self) -> str:
        return (
            f"{self.sample_year_start}-{self.sample_year_end}, "
            f"±{self.calendar_window_days} days around the date, "
            f"{self.sample_count} observations, {self.provider} / {self.dataset}"
        )


class WeatherContext(BaseModel):
    """What is known about one day's weather, and how it is known."""

    date: date_type
    kind: WeatherKind = "unavailable"

    condition: str | None = None
    high_c: float | None = None
    low_c: float | None = None

    # Forecast only: the chance of rain on this date, 0-1.
    precipitation_probability: float | None = None
    # Norm only: the share of comparable days that saw rain, 0-1. Deliberately
    # a different field from the one above - they are different claims and a
    # renderer that shares a field will eventually share a sentence.
    precipitation_day_frequency: float | None = None
    precipitation_mm: float | None = None

    wind_kph: float | None = None
    uv_index: float | None = None

    sunrise: datetime | None = None
    sunset: datetime | None = None

    source: str | None = None
    observed_at: datetime | None = None

    # Present only when `kind == "historical_norm"`.
    norm: ClimatologyMethod | None = None

    # Present only when `kind == "unavailable"`, and says which way it failed.
    unavailable_reason: str | None = None

    @property
    def is_forecast(self) -> bool:
        return self.kind == "forecast"

    @property
    def rain_chance(self) -> float | None:
        """The precipitation figure, whichever kind this is.

        Callers that only need a number for display use this. Callers deciding
        whether to *warn* must check `is_forecast` first - a norm may inform and
        may not warn about a date.
        """
        if self.kind == "forecast":
            return self.precipitation_probability
        if self.kind == "historical_norm":
            return self.precipitation_day_frequency
        return None

    def headline(self) -> str:
        """One line, never mixing the two kinds of claim."""
        if self.kind == "unavailable":
            return self.unavailable_reason or "weather unavailable"
        bits: list[str] = []
        if self.high_c is not None and self.low_c is not None:
            bits.append(f"{self.high_c:.0f}–{self.low_c:.0f}°C")
        elif self.high_c is not None:
            bits.append(f"{self.high_c:.0f}°C")
        chance = self.rain_chance
        if chance is not None:
            bits.append(
                f"Rain {chance * 100:.0f}%"
                if self.kind == "forecast"
                else f"Rain on ~{chance * 100:.0f}% of comparable days"
            )
        if self.wind_kph:
            bits.append(f"Wind {self.wind_kph:.0f} km/h")
        return " · ".join(bits) if bits else "no figures"

    def label(self) -> str:
        """What the reader must be told this is."""
        return {
            "forecast": "Forecast",
            "historical_norm": "Typical weather — historical pattern, not a forecast",
            "unavailable": "Weather unavailable",
        }[self.kind]
