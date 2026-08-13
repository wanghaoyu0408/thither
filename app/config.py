from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "travel-agent"
    debug: bool = False

    # Storage. SQLite for local dev; Postgres is the production target.
    database_url: str = "sqlite+aiosqlite:///./travel_agent.db"
    auto_create_tables: bool = True

    # Google Places (New) + Routes. Required from Milestone 2.
    google_maps_api_key: str | None = None
    google_places_language: str | None = "en"
    google_places_region: str | None = None

    # The key the *browser* loads Maps JavaScript with. A page publishes every
    # key it loads, so this is deliberately a separate setting: the server key
    # above stays private for Places and Routes, while this one is meant to be
    # locked to an HTTP referrer. Left unset the map and place photos simply do
    # not load - the page never falls back to the server key, because a scraped
    # key spends the same quota. See `maps_browser_key`.
    maps_browser_api_key: str | None = None

    # Durable half of the tool cache. Only place ids and lat/lng ever persist;
    # see app/services/cache.py.
    cache_enabled: bool = True

    # LLM. Required from Milestone 3.
    openai_api_key: str | None = None
    openai_model: str = "gpt-5"
    # Hard stop on the agent loop, so a confused model cannot spend forever.
    #
    # Raised from 12 in M6. A five-day plan legitimately spends rounds on
    # discovery, then place details, then generate, then apply; a live run
    # measured 13 tool calls against a 12-round cap, which left no headroom at
    # all and made the acceptance turn on whether the model happened to be
    # economical. This still bounds a confused model - it just stops cutting off
    # a working one.
    agent_max_iterations: int = 16
    # Ceiling on fresh Places searches per planning run; the entity registry is
    # consulted first.
    #
    # This shapes how the model plans - it is told when the budget is gone, and
    # is expected to work with what it has. It is not what stops a bill: the
    # turn budget below is, and the two count different things on purpose.
    planning_search_budget: int = 8

    # Everything a single turn is allowed to spend.
    #
    # `agent_max_iterations` bounds rounds, not cost. A round may emit any
    # number of tool calls, and the reply grows with every tool result fed back
    # in, so a turn's real bill is the tokens it accumulates and the paid calls
    # it makes. Those are the two things capped here.
    #
    # The token ceiling sits well above the most expensive turn ever measured
    # here (280,311 input tokens over 14 rounds, for a legitimate five-day
    # plan). It is a floor under a runaway, not a target for normal work: a turn
    # that reaches it has stopped making progress.
    agent_max_turn_tokens: int = 500_000
    # Paid provider calls per turn, per provider. Cache hits do not count -
    # these are enforced at `request_json`, which only fresh calls reach.
    # See app/providers/base.py for why the counter lives there.
    #
    # Measured against live providers rather than guessed. On a four-day trip
    # with ten places and thirteen stops:
    #
    #   a fresh Places text search        1 call   (and `planning_search_budget`
    #                                               already caps these at 8)
    #   opening hours for ten places     10 calls  (one per place - the term
    #                                               that grows with the trip)
    #   route matrices for four days      2 calls  (the whole stress test)
    #   the same work a second time       0 calls  (cache)
    #
    # So a heavy real turn on that trip costs about 20, and a forty-entity trip
    # extrapolates to about 50. These sit above that and far below a loop.
    turn_google_call_budget: int = 120
    turn_flight_call_budget: int = 20
    turn_hotel_call_budget: int = 20
    # Any provider without a line of its own, so one added later is bounded
    # from its first call rather than exempt until someone remembers it.
    turn_provider_call_budget: int = 50

    # Ceiling on one model reply, and deliberately a loose one.
    #
    # This counts **reasoning tokens too** - in the Responses API they are part
    # of `output_tokens` - so a cap set from visible output alone would cut off
    # a model mid-thought, and a reply whose reasoning ate the entire budget
    # comes back empty. Measured on gpt-5.6-luna against a hard scheduling
    # prompt, worst case over all four reasoning efforts:
    #
    #   effort=low      363 tokens   (105 reasoning + 258 visible)
    #   effort=medium   624 tokens   (377 reasoning + 247 visible)
    #   effort=high     712 tokens   (456 reasoning + 256 visible)
    #
    # Replies stay short because the prompt makes them short: the model calls
    # tools instead of writing essays, and invariant 8 forbids it from redoing
    # the engine's arithmetic in prose.
    #
    # The asymmetry decides the number. A ceiling is not a reservation - unused
    # headroom is never billed - so setting this high costs nothing, while
    # setting it tight silently truncates working turns on a heavier model or a
    # longer written summary. What actually bounds the bill is
    # `agent_max_turn_tokens` above. This only stops one reply running away.
    openai_max_output_tokens: int = 32_000
    # The OpenAI SDK defaults to 600s and retries twice on its own, so a single
    # hung request could hold a turn for half an hour. A round that has not
    # answered in two minutes is not going to.
    openai_request_timeout_seconds: float = 120.0
    openai_max_retries: int = 1

    # How much the worst-served traveller counts in a group score:
    #   total = (1 - w) * mean + w * worst
    # 0.0 is a plain mean, which lets three people vote one into a trip they
    # will hate; 1.0 is pure maximin, which lets one lukewarm person veto
    # everything. Configurable because it is a judgement about the group, not a
    # fact about the options - and it is recorded on every GroupScore so a
    # stored number can be read back correctly.
    group_worst_weight: float = Field(default=0.4, ge=0.0, le=1.0)

    # Flights. Required from Milestone 5.
    duffel_access_token: str | None = None

    # Hotel prices, via Google Hotels. Required from Milestone 6.
    #
    # The spec named Amadeus here. Amadeus Self-Service was decommissioned on
    # 17 July 2026 and Amadeus Enterprise is out of scope for a prototype, so
    # its settings are gone rather than left lying around to be half-configured.
    # Only the HotelProvider interface survived the swap, which was the point of
    # having one.
    serpapi_api_key: str | None = None

    # Google Weather is a separate API from Places and Routes and needs
    # enabling on its own. Unset, the forecast half simply does not run and the
    # historical half still does - Open-Meteo needs no credential.
    weather_api_key: str | None = None

    # Weather thresholds. Deliberately conservative and configurable: this
    # project does not pretend to know when a hike becomes unsafe, only when a
    # traveller would want to be told.
    rain_warning_probability: float = 0.5
    wind_warning_kph: float = 40.0

    # Evidence needed before a learned pattern may be proposed to the
    # traveller. At 1, a single misclick becomes a personality trait; at 10,
    # nothing is ever learned. Signals *and* distinct trips, because one
    # bad-weather week generates any amount of behaviour - a preference is
    # what survives a change of city.
    learning_min_signals: int = 3
    learning_min_trips: int = 2

    # Checks needed before this system will say anything about its own
    # accuracy. Below the first number it reports "never enough to say" and
    # adjusts nothing - three checks of a travel-time estimate is three
    # journeys, one of which may have been a road closure, and "this provider
    # runs 22% low" said on the strength of that is exactly the invented
    # precision the rest of the codebase refuses. Between the two it is shown
    # and still never allowed to move a ranking.
    calibration_min_samples: int = 5
    calibration_confident_samples: int = 12

    @property
    def weather_key(self) -> str | None:
        """The key Google Weather is called with.

        Weather is a Google Maps Platform API, so the same key serves it once
        the Weather API is enabled on the project. The separate setting exists
        for the case where it is not, or where weather should be billed apart.
        """
        return self.weather_api_key or self.google_maps_api_key

    @property
    def maps_browser_key(self) -> str | None:
        """The key to hand the page, or None if the map should not be offered.

        This used to fall back to the server key so the map worked on localhost
        with one key configured. That published the Places and Routes budget to
        anyone who read the page. Now the page gets a key only when one was set
        aside for it: no browser key, no map - a dark map is a smaller loss
        than a spent quota.
        """
        return self.maps_browser_api_key

    @property
    def sync_database_url(self) -> str:
        """Alembic runs migrations over a sync driver."""
        return self.database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
