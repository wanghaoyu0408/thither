"""Weather, and the one line it must never cross.

A forecast is about a date. A norm is about a season. Every test here exists to
stop the second turning into the first — because a seasonal average dressed up
as Tuesday's weather is the most confident kind of wrong answer this system can
produce, and it would look exactly like a real forecast on the screen.
"""

from datetime import UTC, date, datetime, timedelta

from app.models.entity import PlaceEntity
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary
from app.models.trip import TripState
from app.models.weather import ClimatologyMethod, WeatherContext
from app.providers.weather_provider import DailyObservation
from app.services.cache import InProcessCache
from app.services.validation_service import validate_itinerary
from app.services.weather_service import WeatherService

TODAY = date(2026, 8, 9)
MAUI = (20.7984, -156.3319)


class FakeForecast:
    name = "google_weather"
    horizon_days = 8

    def __init__(self, days: int = 8, rain: float = 0.2, wind: float = 5.0):
        self.days, self.rain, self.wind = days, rain, wind
        self.calls = 0

    async def daily_forecast(self, *, lat, lng, days):
        self.calls += 1
        # Like the real one, it starts at the location's yesterday.
        return [
            DailyObservation(
                date=TODAY - timedelta(days=1) + timedelta(days=offset),
                high_c=28.0,
                low_c=20.0,
                precipitation_probability=self.rain,
                wind_kph=self.wind,
                condition="Rain",
                sunrise=f"{(TODAY + timedelta(days=offset)).isoformat()}T16:02:00Z",
                sunset=f"{(TODAY + timedelta(days=offset)).isoformat()}T04:58:00Z",
            )
            for offset in range(self.days)
        ]


class FakeHistory:
    name = "open-meteo"
    dataset = "ERA5-Land / ERA5"

    def __init__(self):
        self.requests: list[tuple[date, date]] = []

    async def daily_history(self, *, lat, lng, start, end):
        self.requests.append((start, end))
        out, cursor = [], start
        while cursor <= end:
            # Every fourth day wet, so the frequency is a known 25%.
            wet = cursor.toordinal() % 4 == 0
            out.append(
                DailyObservation(
                    date=cursor,
                    high_c=29.0 if wet else 31.0,
                    low_c=22.0,
                    precipitation_mm=6.0 if wet else 0.0,
                    wind_kph=18.0,
                )
            )
            cursor += timedelta(days=1)
        return out


def service(forecast=None, history=None) -> WeatherService:
    return WeatherService(forecast, history, InProcessCache())


# --- which kind of answer a date gets ----------------------------------------


async def test_a_date_inside_the_horizon_gets_a_forecast():
    got = await service(FakeForecast(), FakeHistory()).context_for(
        [TODAY + timedelta(days=1)], lat=MAUI[0], lng=MAUI[1], today=TODAY
    )

    context = got[TODAY + timedelta(days=1)]
    assert context.kind == "forecast"
    assert context.label() == "Forecast"
    assert context.norm is None
    assert context.precipitation_day_frequency is None, "a forecast has no frequency"


async def test_a_date_beyond_the_horizon_gets_a_norm_and_never_a_forecast():
    far = date(2027, 3, 15)
    history = FakeHistory()

    got = await service(FakeForecast(), history).context_for(
        [far], lat=MAUI[0], lng=MAUI[1], today=TODAY
    )

    context = got[far]
    assert context.kind == "historical_norm"
    assert "not a forecast" in context.label()
    assert context.precipitation_probability is None, "a norm never carries a date's chance"
    assert context.precipitation_day_frequency == round(56 / 225, 3)
    assert len(history.requests) == 1, "one range fetch, not one per year"


async def test_a_norm_carries_the_method_that_produced_it():
    far = date(2027, 3, 15)

    got = await service(FakeForecast(), FakeHistory()).context_for(
        [far], lat=MAUI[0], lng=MAUI[1], today=TODAY
    )

    method = got[far].norm
    assert method is not None
    assert method.sample_year_end - method.sample_year_start == 14, "fifteen years"
    assert method.calendar_window_days == 7
    assert method.sample_count == 15 * 15
    assert "ERA5" in method.dataset
    assert "2011-2025" in method.describe() and "225 observations" in method.describe()


async def test_no_forecast_provider_still_gives_seasonal_context():
    """Open-Meteo needs no credential, so a machine with no weather key is not
    a machine with no weather - even for tomorrow. A norm about tomorrow is
    still true and still labelled as a norm; the traveller loses precision, not
    honesty."""
    far = date(2027, 3, 15)
    tomorrow = TODAY + timedelta(days=1)

    got = await service(None, FakeHistory()).context_for(
        [far, tomorrow], lat=MAUI[0], lng=MAUI[1], today=TODAY
    )

    assert got[far].kind == "historical_norm"
    assert got[tomorrow].kind == "historical_norm"
    assert "not a forecast" in got[tomorrow].label()
    assert got[tomorrow].precipitation_probability is None


async def test_both_sources_failing_is_unavailable_not_invented():
    got = await service(None, None).context_for(
        [TODAY + timedelta(days=1)], lat=MAUI[0], lng=MAUI[1], today=TODAY
    )

    context = got[TODAY + timedelta(days=1)]
    assert context.kind == "unavailable"
    assert context.high_c is None and context.rain_chance is None
    assert context.label() == "Weather unavailable"


# --- what each kind is allowed to do to the itinerary ------------------------


def outdoor_trip(weather: WeatherContext | None, *, timezone: str | None = "Pacific/Honolulu"):
    state = TripState.new(title="Maui")
    state.brief.timezone = timezone
    state.entities["ent_beach"] = PlaceEntity(
        entity_id="ent_beach", name="Makena Beach", categories=["beach"], lat=20.63, lng=-156.44
    )
    state.itinerary = TripItinerary(
        days=[
            ItineraryDay(
                date=TODAY + timedelta(days=1),
                weather=weather,
                items=[
                    ItineraryItem(
                        item_id="item_beach",
                        type="activity",
                        entity_id="ent_beach",
                        title="Makena Beach",
                        start_at=datetime(2026, 8, 10, 11, 0),
                        end_at=datetime(2026, 8, 10, 13, 0),
                    )
                ],
            )
        ]
    )
    return state


def forecast(**kwargs) -> WeatherContext:
    return WeatherContext(date=TODAY + timedelta(days=1), kind="forecast", **kwargs)


def norm(**kwargs) -> WeatherContext:
    return WeatherContext(
        date=TODAY + timedelta(days=1),
        kind="historical_norm",
        norm=ClimatologyMethod(
            provider="open-meteo",
            dataset="ERA5-Land / ERA5",
            sample_year_start=2011,
            sample_year_end=2025,
            calendar_window_days=7,
            sample_count=225,
        ),
        **kwargs,
    )


def types_of(state):
    return [issue.type for issue in validate_itinerary(state).issues]


def issue(state, kind):
    return next(i for i in validate_itinerary(state).issues if i.type == kind)


def test_a_wet_forecast_warns_about_an_outdoor_day():
    state = outdoor_trip(forecast(precipitation_probability=0.65))

    assert "weather_rain_risk" in types_of(state)
    assert issue(state, "weather_rain_risk").severity == "warning"


def test_a_wet_season_only_informs_and_never_warns():
    """The heart of it. Same number, different kind of claim, different force."""
    state = outdoor_trip(norm(precipitation_day_frequency=0.65))

    raised = issue(state, "weather_seasonal_risk")
    assert raised.severity == "info", "a norm may not warn about a date"
    assert "not a forecast for this date" in raised.message
    assert "weather_rain_risk" not in types_of(state)


def test_a_norm_can_never_produce_an_error():
    state = outdoor_trip(norm(precipitation_day_frequency=0.99, wind_kph=90.0))

    assert not [i for i in validate_itinerary(state).issues if i.severity == "error"]


def test_a_dry_forecast_says_nothing():
    state = outdoor_trip(forecast(precipitation_probability=0.1, wind_kph=5.0))

    assert not [t for t in types_of(state) if t.startswith("weather_")]


def test_wind_warns_only_where_it_is_actually_windy():
    calm = outdoor_trip(forecast(wind_kph=12.0))
    gale = outdoor_trip(forecast(wind_kph=55.0))

    assert "weather_wind_risk" not in types_of(calm)
    assert "weather_wind_risk" in types_of(gale)


def test_an_indoor_day_is_not_warned_about_rain():
    state = outdoor_trip(forecast(precipitation_probability=0.9))
    state.entities["ent_beach"].categories = ["museum"]

    assert "weather_rain_risk" not in types_of(state)


def test_missing_weather_produces_no_issues_at_all():
    assert not [t for t in types_of(outdoor_trip(None)) if t.startswith("weather_")]
    unavailable = outdoor_trip(
        WeatherContext(date=TODAY + timedelta(days=1), kind="unavailable")
    )
    assert not [t for t in types_of(unavailable) if t.startswith("weather_")]


# --- sun events ---------------------------------------------------------------


def sunset_trip(*, hour: int, timezone: str | None = "Pacific/Honolulu"):
    state = outdoor_trip(
        forecast(sunset=datetime(2026, 8, 11, 4, 58, tzinfo=UTC)), timezone=timezone
    )
    item = state.itinerary.days[0].items[0]
    item.title = "Sunset at Makena"
    item.start_at = datetime(2026, 8, 10, hour, 0)
    return state


def test_a_sunset_item_after_sunset_is_an_error():
    """Sunset in Maui on 10 August is 18:58 local; 19:30 is after it."""
    assert "scheduled_after_sunset" in types_of(sunset_trip(hour=19))
    assert issue(sunset_trip(hour=19), "scheduled_after_sunset").severity == "error"


def test_a_sunset_item_before_sunset_is_fine():
    assert "scheduled_after_sunset" not in types_of(sunset_trip(hour=18))


def test_why_carries_the_days_weather_as_context_not_as_a_reason():
    """Nothing ranks on weather, so presenting it as why something was chosen
    would be exactly the invented rationale this module exists to prevent."""
    from app.services.explanation_service import explain_item

    state = outdoor_trip(forecast(precipitation_probability=0.65, high_c=28.0))

    explanation = explain_item(state, "item_beach")

    assert explanation.day_weather is not None
    assert explanation.day_weather.kind == "forecast"
    # And it is not smuggled into the reasons.
    assert not any("rain" in reason.lower() for reason in explanation.pros + explanation.cons)


def test_sun_events_are_not_checked_without_the_destination_timezone():
    """A UTC sun event against a naive local time is wrong by whole hours, so
    the check does not run rather than running on a guess."""
    assert "scheduled_after_sunset" not in types_of(sunset_trip(hour=19, timezone=None))
