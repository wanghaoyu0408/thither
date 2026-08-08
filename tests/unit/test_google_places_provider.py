"""Field-mask construction and response normalization, offline."""

import pytest

from app.models.place import PlaceFieldSet
from app.providers.google_places import (
    details_field_mask,
    fields_for,
    normalize_place,
    search_field_mask,
)

# A trimmed but structurally faithful Text Search (New) response.
RAW_PLACE = {
    "id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
    "displayName": {"text": "Fuglen Tokyo", "languageCode": "en"},
    "formattedAddress": "1-16-11 Tomigaya, Shibuya City, Tokyo",
    "location": {"latitude": 35.6659, "longitude": 139.6979},
    "types": ["cafe", "bar", "food", "point_of_interest"],
    "primaryType": "cafe",
    "rating": 4.3,
    "userRatingCount": 2317,
    "priceLevel": "PRICE_LEVEL_MODERATE",
    "googleMapsUri": "https://maps.google.com/?cid=123",
    "websiteUri": "https://fuglen.com",
    "businessStatus": "OPERATIONAL",
    "regularOpeningHours": {"openNow": True, "weekdayDescriptions": ["Monday: 8:00 AM - 10:00 PM"]},
}


def test_tiers_are_cumulative():
    assert fields_for(PlaceFieldSet.IDS) == ["id"]

    basic = fields_for(PlaceFieldSet.BASIC)
    assert "id" in basic and "displayName" in basic
    assert "rating" not in basic

    ranking = fields_for(PlaceFieldSet.RANKING)
    assert set(basic) < set(ranking)
    assert {"rating", "userRatingCount", "priceLevel"} <= set(ranking)

    full = fields_for(PlaceFieldSet.FULL)
    assert set(ranking) < set(full)
    assert {"regularOpeningHours", "websiteUri"} <= set(full)


def test_ranking_tier_carries_what_ranking_needs():
    """Ranking cannot run on a Pro-tier mask; this is why the tier is explicit."""
    assert {"rating", "userRatingCount"} <= set(fields_for(PlaceFieldSet.RANKING))
    assert {"rating", "userRatingCount"}.isdisjoint(fields_for(PlaceFieldSet.BASIC))


def test_search_mask_is_prefixed_and_details_mask_is_not():
    search = search_field_mask(PlaceFieldSet.BASIC)
    details = details_field_mask(PlaceFieldSet.BASIC)

    assert search.startswith("places.id,")
    assert all(part.startswith("places.") for part in search.split(","))
    assert details.startswith("id,")
    assert "places." not in details


@pytest.mark.parametrize("mask_builder", [search_field_mask, details_field_mask])
def test_masks_contain_no_spaces(mask_builder):
    # Google rejects a field mask containing whitespace.
    assert " " not in mask_builder(PlaceFieldSet.FULL)


def test_normalize_maps_every_field():
    place = normalize_place(RAW_PLACE)

    assert place.place_id == "ChIJN1t_tDeuEmsRUsoyG83frY4"
    assert place.name == "Fuglen Tokyo"
    assert place.address.startswith("1-16-11 Tomigaya")
    assert (place.lat, place.lng) == (35.6659, 139.6979)
    assert place.primary_type == "cafe"
    assert place.rating == 4.3
    assert place.rating_count == 2317
    assert place.price_level == 2
    assert place.website_url == "https://fuglen.com"
    assert place.opening_hours["openNow"] is True
    assert place.is_operational()


def test_absent_fields_stay_none_rather_than_zero():
    """A Pro-tier search omits rating; None must not be read as a bad rating."""
    place = normalize_place({"id": "abc", "displayName": {"text": "Somewhere"}})

    assert place.rating is None
    assert place.rating_count is None
    assert place.price_level is None
    assert place.lat is None


def test_unknown_price_enum_becomes_none():
    place = normalize_place({"id": "abc", "priceLevel": "PRICE_LEVEL_UNSPECIFIED"})

    assert place.price_level is None


def test_closed_business_is_flagged():
    place = normalize_place({"id": "abc", "businessStatus": "CLOSED_PERMANENTLY"})

    assert place.is_operational() is False


def test_place_id_recovered_from_resource_name():
    place = normalize_place({"name": "places/ChIJ_abc123", "displayName": {"text": "X"}})

    assert place.place_id == "ChIJ_abc123"
