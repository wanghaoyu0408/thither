"""Open-Meteo's archive: observed weather, for working out what is typical.

Reanalysis (ERA5-Land where it covers the point, ERA5 otherwise), not a
forecast and not a climate projection. What comes back is what the weather
actually did, and the climatology is computed from it in
`app/services/weather_service.py` - so the method behind "typical high 29C" is
ours and can be printed alongside the number.

No API key. The free non-commercial tier does not require one, and inventing a
setting for a credential that does not exist would be a lie in the config file.
"""

from datetime import date as date_type

import httpx

from app.providers.base import request_json
from app.providers.weather_provider import DailyObservation

PROVIDER = "open-meteo"
BASE_URL = "https://archive-api.open-meteo.com/v1/archive"

# ERA5-Land is the finer grid and covers land surfaces; ERA5 is the fallback
# Open-Meteo uses where it does not. Recorded so a norm says which it rests on.
DATASET = "ERA5-Land / ERA5"

_DAILY = (
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "wind_speed_10m_max",
)


class OpenMeteoHistoricalProvider:
    name = PROVIDER
    dataset = DATASET

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def daily_history(
        self, *, lat: float, lng: float, start: date_type, end: date_type
    ) -> list[DailyObservation]:
        payload = await request_json(
            self._client,
            "GET",
            BASE_URL,
            provider=PROVIDER,
            params={
                "latitude": lat,
                "longitude": lng,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "daily": ",".join(_DAILY),
                "timezone": "UTC",
                "wind_speed_unit": "kmh",
            },
        )

        daily = payload.get("daily") or {}
        days = daily.get("time") or []
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        rain = daily.get("precipitation_sum") or []
        wind = daily.get("wind_speed_10m_max") or []

        def at(series: list, index: int):
            return series[index] if index < len(series) else None

        observations: list[DailyObservation] = []
        for index, stamp in enumerate(days):
            try:
                when = date_type.fromisoformat(stamp)
            except (TypeError, ValueError):
                continue
            observations.append(
                DailyObservation(
                    date=when,
                    high_c=at(highs, index),
                    low_c=at(lows, index),
                    precipitation_mm=at(rain, index),
                    wind_kph=at(wind, index),
                )
            )
        return observations
