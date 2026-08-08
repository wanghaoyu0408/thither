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

    # Placeholders for later milestones.
    openai_api_key: str | None = None
    duffel_access_token: str | None = None
    amadeus_client_id: str | None = None
    amadeus_client_secret: str | None = None
    weather_api_key: str | None = None

    @property
    def sync_database_url(self) -> str:
        """Alembic runs migrations over a sync driver."""
        return self.database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
