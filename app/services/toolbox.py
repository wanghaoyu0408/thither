"""Assembles the external-data tools for one run or one request.

Owns the HTTP client, the providers, the cache stack and the deduper so that
callers - scripts, API endpoints, and the agent loop in M3 - never wire Google
credentials themselves.
"""

from types import TracebackType

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, get_settings
from app.providers.base import build_client
from app.providers.duffel import DuffelProvider
from app.providers.google_places import GooglePlacesProvider
from app.providers.google_routes import GoogleRoutesProvider
from app.providers.google_weather import GoogleWeatherProvider
from app.providers.open_meteo import OpenMeteoHistoricalProvider
from app.providers.openai_research import OpenAIResearchProvider
from app.providers.serpapi_hotels import SerpApiGoogleHotelsProvider
from app.services.airport_service import AirportService
from app.services.cache import InProcessCache, LayeredCache, RequestDeduper, SqliteCache
from app.services.discovery_service import DiscoveryService
from app.services.flight_service import FlightService
from app.services.hotel_area_service import HotelAreaService
from app.services.hotel_service import HotelService
from app.services.parking_service import ParkingService
from app.services.place_service import PlaceService
from app.services.research_service import ResearchService
from app.services.route_service import RouteService
from app.services.weather_service import WeatherService


class MissingCredentials(RuntimeError):
    pass


class Toolbox:
    def __init__(
        self,
        settings: Settings | None = None,
        sessions: async_sessionmaker | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        if not self._settings.google_maps_api_key:
            raise MissingCredentials(
                "GOOGLE_MAPS_API_KEY is not set. Copy .env.example to .env and add a key with "
                "Places API (New) and Routes API enabled."
            )

        self._client = build_client()

        durable = (
            SqliteCache(sessions) if sessions is not None and self._settings.cache_enabled else None
        )
        self._cache = LayeredCache(InProcessCache(), durable)
        self._deduper = RequestDeduper()

        key = self._settings.google_maps_api_key

        self.places = PlaceService(
            GooglePlacesProvider(key, self._client),
            self._cache,
            self._deduper,
            language=self._settings.google_places_language,
            region=self._settings.google_places_region,
        )
        self.routes = RouteService(
            GoogleRoutesProvider(key, self._client),
            self._cache,
            self._deduper,
        )

        # Research needs an LLM key. Without one the discovery pipeline still
        # runs - on Google data alone, and saying so (spec section 36).
        self.research: ResearchService | None = None
        if self._settings.openai_api_key:
            self.research = ResearchService(
                OpenAIResearchProvider(self._settings.openai_api_key, self._settings.openai_model),
                self._cache,
                self._deduper,
            )

        self.discovery = DiscoveryService(self.places, self.research)
        # Parking needs only Google, like the neighbourhood half of hotels.
        self.parking = ParkingService(self.places, self.routes)
        self.airports = AirportService(self.places, self.routes)

        # Flights need their own credential. Without one the agent says flights
        # cannot be searched rather than inventing fares.
        self.flights: FlightService | None = None
        if self._settings.duffel_access_token:
            self.flights = FlightService(
                DuffelProvider(self._settings.duffel_access_token, self._client),
                self._cache,
                self._deduper,
            )

        # Choosing a neighbourhood needs only Google, so it works whether or not
        # a hotel price provider is configured - and it is the half of spec
        # section 25 that has to happen first anyway.
        self.hotel_areas = HotelAreaService(self.places, self.routes, self.research)

        # Hotel prices need their own credential, same rule as flights.
        self.hotels: HotelService | None = None
        if self._settings.serpapi_api_key:
            self.hotels = HotelService(
                SerpApiGoogleHotelsProvider(self._settings.serpapi_api_key, self._client),
                self.places,
                self.routes,
                self._cache,
                self._deduper,
            )

        # Weather has two halves and they fail independently. Google answers
        # about dates inside its horizon; Open-Meteo needs no credential at all,
        # so a trip months out still gets its seasonal context even on a machine
        # with no weather key.
        self.weather = WeatherService(
            GoogleWeatherProvider(self._settings.weather_key, self._client)
            if self._settings.weather_key
            else None,
            OpenMeteoHistoricalProvider(self._client),
            self._cache,
            self._deduper,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "Toolbox":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
