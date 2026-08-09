"""Google Weather: the daily forecast, for dates inside its horizon.

Only the forecast. Google publishes current conditions and a short history too,
and neither is what this project needs: the history is 24 hours, which is no
basis for a norm, and current conditions are about now rather than about a day
of the trip.

Like every provider here it raises on failure rather than returning something
plausible - `request_json` classifies the error and the service above decides
what the traveller is told.
"""

from datetime import date as date_type
from typing import Any

import httpx

from app.providers.base import request_json
from app.providers.weather_provider import DailyObservation

PROVIDER = "google_weather"
BASE_URL = "https://weather.googleapis.com/v1"

# Google publishes 10 days, but the run starts at the *location's* yesterday -
# which can be two days behind UTC today for a place like Hawaii. So the
# furthest date it can actually answer about is nearer eight days out than ten,
# and advertising ten would have the service asking for forecasts that come back
# uncovered instead of falling through to a seasonal norm that would have said
# something useful.
API_DAYS = 10
LEAD_DAYS = 2
HORIZON_DAYS = API_DAYS - LEAD_DAYS
# The API's own page cap, whatever `days` asks for.
PAGE_SIZE = 5
# Enough pages to cover the horizon, and a bound so a paging bug cannot loop.
MAX_PAGES = 4


def _celsius(block: Any) -> float | None:
    if not isinstance(block, dict):
        return None
    degrees = block.get("degrees")
    if degrees is None:
        return None
    return float(degrees) if block.get("unit", "CELSIUS") == "CELSIUS" else None


def _condition(daytime: dict[str, Any]) -> str | None:
    description = (daytime.get("weatherCondition") or {}).get("description") or {}
    return description.get("text")


def _iso(block: Any) -> str | None:
    return block if isinstance(block, str) else None


def _day_of(entry: dict[str, Any]) -> date_type | None:
    display = entry.get("displayDate") or {}
    try:
        return date_type(int(display["year"]), int(display["month"]), int(display["day"]))
    except (KeyError, TypeError, ValueError):
        return None


class GoogleWeatherProvider:
    name = PROVIDER
    horizon_days = HORIZON_DAYS

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._client = client

    async def daily_forecast(
        self, *, lat: float, lng: float, days: int
    ) -> list[DailyObservation]:
        # Ask past the caller's span, because the days Google spends on the
        # location's own yesterday are days it does not spend on the trip.
        wanted = min(days + LEAD_DAYS, API_DAYS)
        entries: list[dict[str, Any]] = []
        token: str | None = None

        # Google returns at most PAGE_SIZE days per call whatever `days` says,
        # and starts from the location's own yesterday - so a five-day trip
        # needs more than one page even though five is under the horizon. Asking
        # once and taking what came back silently lost the last two days of the
        # Maui trip.
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "key": self._api_key,
                "location.latitude": lat,
                "location.longitude": lng,
                "days": wanted,
                "pageSize": PAGE_SIZE,
                "unitsSystem": "METRIC",
            }
            if token:
                params["pageToken"] = token
            payload = await request_json(
                self._client,
                "GET",
                f"{BASE_URL}/forecast/days:lookup",
                provider=PROVIDER,
                params=params,
            )
            page = payload.get("forecastDays") or []
            entries.extend(page)
            token = payload.get("nextPageToken")
            if not token or len(entries) >= wanted + 1:
                break

        observations: list[DailyObservation] = []
        for entry in entries:
            when = _day_of(entry)
            if when is None:
                continue
            daytime = entry.get("daytimeForecast") or {}
            rain = daytime.get("precipitation") or {}
            probability = (rain.get("probability") or {}).get("percent")
            wind = (daytime.get("wind") or {}).get("speed") or {}
            sun = entry.get("sunEvents") or {}

            observations.append(
                DailyObservation(
                    date=when,
                    high_c=_celsius(entry.get("maxTemperature")),
                    low_c=_celsius(entry.get("minTemperature")),
                    precipitation_probability=(
                        float(probability) / 100.0 if probability is not None else None
                    ),
                    precipitation_mm=((rain.get("qpf") or {}).get("quantity")),
                    wind_kph=(float(wind["value"]) if wind.get("value") is not None else None),
                    uv_index=(
                        float(daytime["uvIndex"]) if daytime.get("uvIndex") is not None else None
                    ),
                    condition=_condition(daytime),
                    sunrise=_iso(sun.get("sunriseTime")),
                    sunset=_iso(sun.get("sunsetTime")),
                )
            )
        return observations
