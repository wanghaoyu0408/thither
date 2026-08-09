"""Which kind of weather answer a date can have, and the arithmetic behind a norm.

One rule decides everything here: **a date inside the forecast horizon gets a
forecast; a date outside it gets a climatology, and a climatology is never
presented as a forecast.** Everything else is bookkeeping.

The climatology is computed here rather than fetched, so the method is code
somebody can read: the same calendar date give or take a week, over the last
fifteen years, aggregated with medians. Medians rather than means because one
hurricane should not move the typical high, and because the number is going to
be read as "a normal day here", which is what a median is.

Every figure it produces carries `ClimatologyMethod`, so "typical high 29°C"
never appears without the years, the window, the sample size and the dataset it
came from.
"""

from datetime import date as date_type
from datetime import datetime, timedelta
from statistics import median

from app.models.common import utcnow
from app.models.weather import ClimatologyMethod, WeatherContext
from app.providers.base import ProviderError
from app.providers.weather_provider import (
    DailyObservation,
    ForecastProvider,
    HistoryProvider,
)
from app.services.cache import CLIMATE_POLICY, VOLATILE_POLICY, Cache, RequestDeduper, cache_key

# How the norm is built. Changing any of these changes what "typical" means, so
# they travel with every number produced from them.
SAMPLE_YEARS = 15
CALENDAR_WINDOW_DAYS = 7
# Reanalysis lags real time; asking for last week returns gaps.
ARCHIVE_LAG_DAYS = 10
# A day counts as wet at or above this. Below it is drizzle nobody reschedules for.
WET_DAY_MM = 1.0


def _round(value: float | None, places: int = 1) -> float | None:
    return None if value is None else round(value, places)


class WeatherService:
    """Weather for a set of dates at a point, each labelled with what it is."""

    def __init__(
        self,
        forecast: ForecastProvider | None,
        history: HistoryProvider | None,
        cache: Cache,
        deduper: RequestDeduper | None = None,
    ) -> None:
        self._forecast = forecast
        self._history = history
        self._cache = cache
        self._deduper = deduper or RequestDeduper()

    async def context_for(
        self,
        dates: list[date_type],
        *,
        lat: float,
        lng: float,
        today: date_type,
    ) -> dict[date_type, WeatherContext]:
        """One context per date. `today` is passed in, never read from a clock
        in here, so the horizon a test exercises is the horizon it asked for."""
        if not dates:
            return {}

        # With no forecast provider the horizon is zero and everything falls
        # through to a norm - including tomorrow. That is the right answer: a
        # norm about tomorrow is still true and still labelled a norm, so the
        # traveller loses precision rather than honesty.
        horizon = self._forecast.horizon_days if self._forecast else 0
        within = [d for d in dates if today <= d <= today + timedelta(days=horizon)]
        beyond = [d for d in dates if d not in set(within)]

        out: dict[date_type, WeatherContext] = {}
        if within:
            out.update(await self._forecasts(within, lat=lat, lng=lng, today=today))
        for when in beyond:
            out[when] = await self._norm(when, lat=lat, lng=lng, today=today)
        for when in dates:
            out.setdefault(
                when,
                WeatherContext(
                    date=when, kind="unavailable", unavailable_reason="no weather source answered"
                ),
            )
        return out

    # --- forecast -------------------------------------------------------------

    async def _forecasts(
        self, dates: list[date_type], *, lat: float, lng: float, today: date_type
    ) -> dict[date_type, WeatherContext]:
        if self._forecast is None:
            return {
                when: WeatherContext(
                    date=when,
                    kind="unavailable",
                    unavailable_reason="no forecast provider is configured",
                )
                for when in dates
            }

        span = (max(dates) - today).days + 1
        key = cache_key("weather:forecast", {"lat": lat, "lng": lng, "days": span})

        cached = await self._cache.get(key, VOLATILE_POLICY)
        if cached is None:
            try:
                observations = await self._deduper.run(
                    key,
                    lambda: self._forecast.daily_forecast(lat=lat, lng=lng, days=span),
                )
            except ProviderError as exc:
                return {
                    when: WeatherContext(
                        date=when,
                        kind="unavailable",
                        unavailable_reason=f"the forecast lookup failed: {exc}",
                    )
                    for when in dates
                }
            await self._cache.set(
                key, [o.model_dump(mode="json") for o in observations], VOLATILE_POLICY
            )
        else:
            observations = [DailyObservation.model_validate(row) for row in cached]

        by_date = {observation.date: observation for observation in observations}
        stamped = utcnow()
        out: dict[date_type, WeatherContext] = {}
        for when in dates:
            observation = by_date.get(when)
            if observation is None:
                out[when] = WeatherContext(
                    date=when,
                    kind="unavailable",
                    unavailable_reason="the forecast did not cover this date",
                )
                continue
            out[when] = WeatherContext(
                date=when,
                kind="forecast",
                condition=observation.condition,
                high_c=_round(observation.high_c),
                low_c=_round(observation.low_c),
                precipitation_probability=observation.precipitation_probability,
                precipitation_mm=observation.precipitation_mm,
                wind_kph=_round(observation.wind_kph),
                uv_index=observation.uv_index,
                sunrise=_parse(observation.sunrise),
                sunset=_parse(observation.sunset),
                source=self._forecast.name,
                observed_at=stamped,
            )
        return out

    # --- the norm -------------------------------------------------------------

    async def _norm(
        self, when: date_type, *, lat: float, lng: float, today: date_type
    ) -> WeatherContext:
        if self._history is None:
            return WeatherContext(
                date=when,
                kind="unavailable",
                unavailable_reason="no historical weather source is configured",
            )

        last_year = min(when.year, today.year) - 1
        first_year = last_year - SAMPLE_YEARS + 1
        key = cache_key(
            "weather:norm",
            {
                "lat": round(lat, 2),
                "lng": round(lng, 2),
                "md": [when.month, when.day],
                "years": [first_year, last_year],
                "window": CALENDAR_WINDOW_DAYS,
            },
        )
        cached = await self._cache.get(key, CLIMATE_POLICY)
        if cached is not None:
            return WeatherContext.model_validate(cached)

        try:
            samples = await self._collect(when, lat=lat, lng=lng, years=(first_year, last_year))
        except ProviderError as exc:
            return WeatherContext(
                date=when,
                kind="unavailable",
                unavailable_reason=f"the historical lookup failed: {exc}",
            )

        if not samples:
            return WeatherContext(
                date=when,
                kind="unavailable",
                unavailable_reason="no historical observations were returned for this place",
            )

        context = _aggregate(
            when,
            samples,
            method=ClimatologyMethod(
                provider=self._history.name,
                dataset=self._history.dataset,
                sample_year_start=first_year,
                sample_year_end=last_year,
                calendar_window_days=CALENDAR_WINDOW_DAYS,
                sample_count=len(samples),
            ),
        )
        # Norms are stable - last decade's Augusts do not change - so unlike a
        # forecast this is worth keeping.
        await self._cache.set(key, context.model_dump(mode="json"), CLIMATE_POLICY)
        return context

    async def _collect(
        self, when: date_type, *, lat: float, lng: float, years: tuple[int, int]
    ) -> list[DailyObservation]:
        """The same calendar window from each of the sample years.

        One request for the whole span, filtered here, rather than fifteen for
        fifteen windows. The archive is a free service and the answer is the
        same either way.
        """
        first_year, last_year = years
        cutoff = utcnow().date() - timedelta(days=ARCHIVE_LAG_DAYS)

        wanted: set[date_type] = set()
        for year in range(first_year, last_year + 1):
            try:
                centre = when.replace(year=year)
            except ValueError:
                # 29 February in a year that has none.
                continue
            for offset in range(-CALENDAR_WINDOW_DAYS, CALENDAR_WINDOW_DAYS + 1):
                day = centre + timedelta(days=offset)
                if day <= cutoff:
                    wanted.add(day)
        if not wanted:
            return []

        observations = await self._history.daily_history(
            lat=lat, lng=lng, start=min(wanted), end=max(wanted)
        )
        return [observation for observation in observations if observation.date in wanted]


def _aggregate(
    when: date_type, samples: list[DailyObservation], *, method: ClimatologyMethod
) -> WeatherContext:
    """Medians, and the share of days that were wet."""
    highs = [s.high_c for s in samples if s.high_c is not None]
    lows = [s.low_c for s in samples if s.low_c is not None]
    winds = [s.wind_kph for s in samples if s.wind_kph is not None]
    rain = [s.precipitation_mm for s in samples if s.precipitation_mm is not None]

    wet = [value for value in rain if value >= WET_DAY_MM]
    return WeatherContext(
        date=when,
        kind="historical_norm",
        high_c=_round(median(highs)) if highs else None,
        low_c=_round(median(lows)) if lows else None,
        wind_kph=_round(median(winds)) if winds else None,
        # The share of comparable days that saw rain - not a chance of rain on
        # this date, which nobody knows.
        precipitation_day_frequency=(round(len(wet) / len(rain), 3) if rain else None),
        # Only over the days it actually rained; averaging in the dry ones would
        # describe a drizzle that never falls.
        precipitation_mm=_round(median(wet)) if wet else None,
        source=method.provider,
        norm=method,
    )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
