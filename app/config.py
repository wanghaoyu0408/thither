from functools import lru_cache

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

    # Durable half of the tool cache. Only place ids and lat/lng ever persist;
    # see app/services/cache.py.
    cache_enabled: bool = True

    # LLM. Required from Milestone 3.
    openai_api_key: str | None = None
    openai_model: str = "gpt-5"
    # Hard stop on the agent loop, so a confused model cannot spend forever.
    agent_max_iterations: int = 12
    # Ceiling on fresh Places searches per planning run; the entity registry is
    # consulted first.
    planning_search_budget: int = 8

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
    def sync_database_url(self) -> str:
        """Alembic runs migrations over a sync driver."""
        return self.database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
