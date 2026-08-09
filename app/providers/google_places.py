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
        # Needed to interpret opening hours as local wall-clock time.
        "timeZone",
    ),
    PlaceFieldSet.RANKING: ("rating", "userRatingCount", "priceLevel"),
    # servesVegetarianFood and accessibilityOptions bill in the same SKU as
    # regularOpeningHours, so adding them here costs nothing extra - and FULL is
    # already fetched for the shortlist only, which is where dietary fit
    # actually needs answering.
    PlaceFieldSet.FULL: (
        "regularOpeningHours",
        "websiteUri",
        "servesVegetarianFood",
        "accessibilityOptions",
    ),
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


def _attested(value: Any) -> bool | None:
    """Google's booleans are one-directional; only `True` is a claim.

    `servesVegetarianFood: false` is returned for every pizzeria in Shibuya, and
    every pizzeria serves a margherita. The field means "attested" or "not
    attested" - never "confirmed absent" - so the false case is folded into
    None, where the rest of this codebase already knows how to treat an unknown.
    """
    return True if value is True else None


def _accessibility(raw: dict[str, Any]) -> dict[str, bool]:
    """Only the accessibility keys Google positively attested.

    Same asymmetry: an all-false object turns up beside half-true ones on
    comparable venues, so a false here is silence rather than a denial.
    """
    options = raw.get("accessibilityOptions")
    if not isinstance(options, dict):
        return {}
    return {key: True for key, value in options.items() if value is True}


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
        serves_vegetarian=_attested(raw.get("servesVegetarianFood")),
        accessibility=_accessibility(raw),
        website_url=raw.get("websiteUri"),
        maps_url=raw.get("googleMapsUri"),
        business_status=raw.get("businessStatus"),
        timezone=(raw.get("timeZone") or {}).get("id"),
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
