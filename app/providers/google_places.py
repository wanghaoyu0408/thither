"""Google Places API (New).

Endpoints:
    POST https://places.googleapis.com/v1/places:searchText
    POST https://places.googleapis.com/v1/places:searchNearby
    GET  https://places.googleapis.com/v1/places/{place_id}

Every request must carry an `X-Goog-FieldMask`, and Google bills by the most
expensive field in it - so the mask is built from an explicit tier rather than
a constant. Note the asymmetry: search masks are prefixed `places.`, details
masks are not.
"""

from typing import Any

import httpx

from app.models.place import PlaceFieldSet, PlaceSummary
from app.providers.base import request_json

PROVIDER = "google_places"
BASE_URL = "https://places.googleapis.com/v1"

# Cumulative tiers; each includes the ones above it.
_FIELDS_BY_TIER: dict[PlaceFieldSet, tuple[str, ...]] = {
    PlaceFieldSet.IDS: ("id",),
    PlaceFieldSet.BASIC: (
        "displayName",
        "formattedAddress",
        "location",
        "types",
        "primaryType",
        "googleMapsUri",
        "businessStatus",
    ),
    PlaceFieldSet.RANKING: ("rating", "userRatingCount", "priceLevel"),
    PlaceFieldSet.FULL: ("regularOpeningHours", "websiteUri"),
}

_TIER_ORDER = (
    PlaceFieldSet.IDS,
    PlaceFieldSet.BASIC,
    PlaceFieldSet.RANKING,
    PlaceFieldSet.FULL,
)

# Google's price enum -> the 0-4 integer PlaceEntity stores.
_PRICE_LEVELS: dict[str, int] = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


def fields_for(field_set: PlaceFieldSet) -> list[str]:
    """Flatten the requested tier and everything below it."""
    fields: list[str] = []
    for tier in _TIER_ORDER:
        fields.extend(_FIELDS_BY_TIER[tier])
        if tier == field_set:
            break
    return fields


def search_field_mask(field_set: PlaceFieldSet) -> str:
    # Search responses nest results under `places`; spaces are rejected.
    return ",".join(f"places.{field}" for field in fields_for(field_set))


def details_field_mask(field_set: PlaceFieldSet) -> str:
    # Details responses are the place itself - no prefix.
    return ",".join(fields_for(field_set))


def normalize_place(raw: dict[str, Any]) -> PlaceSummary:
    """Google's shape -> ours. Absent fields stay None rather than becoming 0."""
    location = raw.get("location") or {}
    opening = raw.get("regularOpeningHours")

    return PlaceSummary(
        place_id=raw.get("id") or raw.get("name", "").rsplit("/", 1)[-1],
        name=(raw.get("displayName") or {}).get("text"),
        address=raw.get("formattedAddress"),
        lat=location.get("latitude"),
        lng=location.get("longitude"),
        categories=raw.get("types") or [],
        primary_type=raw.get("primaryType"),
        rating=raw.get("rating"),
        rating_count=raw.get("userRatingCount"),
        price_level=_PRICE_LEVELS.get(raw.get("priceLevel", "")),
        opening_hours=opening,
        website_url=raw.get("websiteUri"),
        maps_url=raw.get("googleMapsUri"),
        business_status=raw.get("businessStatus"),
    )


class GooglePlacesProvider:
    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._client = client

    def _headers(self, field_mask: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": field_mask,
        }

    async def search_text(
        self,
        *,
        text_query: str,
        field_set: PlaceFieldSet,
        lat: float | None = None,
        lng: float | None = None,
        radius_meters: int | None = None,
        included_type: str | None = None,
        min_rating: float | None = None,
        price_levels: list[int] | None = None,
        open_now: bool | None = None,
        limit: int = 20,
        language: str | None = None,
        region: str | None = None,
    ) -> list[PlaceSummary]:
        body: dict[str, Any] = {"textQuery": text_query, "pageSize": min(limit, 20)}

        if lat is not None and lng is not None and radius_meters:
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_meters),
                }
            }
        if included_type:
            body["includedType"] = included_type
        if min_rating is not None:
            # Google only accepts 0.0-5.0 in 0.5 steps.
            body["minRating"] = round(min_rating * 2) / 2
        if price_levels:
            body["priceLevels"] = [
                name for name, value in _PRICE_LEVELS.items() if value in price_levels
            ]
        if open_now is not None:
            body["openNow"] = open_now
        if language:
            body["languageCode"] = language
        if region:
            body["regionCode"] = region

        payload = await request_json(
            self._client,
            "POST",
            f"{BASE_URL}/places:searchText",
            provider=PROVIDER,
            headers=self._headers(search_field_mask(field_set)),
            json_body=body,
        )
        return [normalize_place(place) for place in payload.get("places", [])]

    async def search_nearby(
        self,
        *,
        lat: float,
        lng: float,
        radius_meters: int,
        field_set: PlaceFieldSet,
        included_types: list[str] | None = None,
        limit: int = 20,
        language: str | None = None,
        region: str | None = None,
    ) -> list[PlaceSummary]:
        body: dict[str, Any] = {
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_meters),
                }
            },
            "maxResultCount": min(limit, 20),
        }
        if included_types:
            body["includedTypes"] = included_types
        if language:
            body["languageCode"] = language
        if region:
            body["regionCode"] = region

        payload = await request_json(
            self._client,
            "POST",
            f"{BASE_URL}/places:searchNearby",
            provider=PROVIDER,
            headers=self._headers(search_field_mask(field_set)),
            json_body=body,
        )
        return [normalize_place(place) for place in payload.get("places", [])]

    async def get_details(
        self,
        place_id: str,
        *,
        field_set: PlaceFieldSet = PlaceFieldSet.FULL,
        language: str | None = None,
    ) -> PlaceSummary:
        params = {"languageCode": language} if language else None
        payload = await request_json(
            self._client,
            "GET",
            f"{BASE_URL}/places/{place_id}",
            provider=PROVIDER,
            headers=self._headers(details_field_mask(field_set)),
            params=params,
        )
        # A details response omits `id` unless asked; keep the id we know.
        payload.setdefault("id", place_id)
        return normalize_place(payload)
