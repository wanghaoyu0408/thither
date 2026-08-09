"""The two shapes of weather source, kept apart on purpose.

A forecast provider answers about dates. A history provider answers about the
past, and the *service* turns that into a norm - deliberately, so the
climatology is ours, computed by code we can point at, rather than a number a
vendor hands over with no method attached.

Two protocols rather than one because they are not interchangeable. Nothing
should be able to satisfy "give me the forecast" with historical observations.
"""

from datetime import date as date_type
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class DailyObservation(BaseModel):
    """One day, as a provider reported it. Every figure optional."""

    date: date_type
    high_c: float | None = None
    low_c: float | None = None
    precipitation_mm: float | None = None
    precipitation_probability: float | None = None
    wind_kph: float | None = None
    uv_index: float | None = None
    condition: str | None = None
    sunrise: str | None = None
    sunset: str | None = None


@runtime_checkable
class ForecastProvider(Protocol):
    """Weather for dates that have not happened yet."""

    name: str
    # How far ahead this provider will answer for. Beyond it the service stops
    # asking rather than accepting whatever comes back.
    horizon_days: int

    async def daily_forecast(
        self, *, lat: float, lng: float, days: int
    ) -> list[DailyObservation]: ...


@runtime_checkable
class HistoryProvider(Protocol):
    """Observed weather for dates that have. Never a forecast."""

    name: str
    dataset: str

    async def daily_history(
        self, *, lat: float, lng: float, start: date_type, end: date_type
    ) -> list[DailyObservation]: ...
