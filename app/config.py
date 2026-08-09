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
    # above can stay unrestricted for Places and Routes, while this one can be
    # locked to an HTTP referrer. Left unset it falls back to the server key,
    # which is fine on localhost and not fine anywhere reachable - a scraped
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
    planning_search_budget: int = 8

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

    # Placeholder for a later milestone.
    weather_api_key: str | None = None

    @property
    def maps_browser_key(self) -> str | None:
        """The key to hand the page, or None if the map should not be offered.

        One resolution point, so nothing else has to remember the fallback.
        """
        return self.maps_browser_api_key or self.google_maps_api_key

    @property
    def maps_key_is_shared_with_the_server(self) -> bool:
        """True when the page publishes the same key the server plans with.

        Worth naming rather than inferring: it is the difference between a key
        that leaks a map and a key that leaks the Places and Routes budget.
        """
        return bool(
            self.google_maps_api_key
            and not self.maps_browser_api_key
        )

    @property
    def sync_database_url(self) -> str:
        """Alembic runs migrations over a sync driver."""
        return self.database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
