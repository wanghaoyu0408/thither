"""SerpApi Google Hotels normalization, against a response shaped like a real one."""

from datetime import date

import pytest

from app.models.common import Money
from app.models.hotel import SearchHotelsInput
from app.providers.base import ProviderBadRequest
from app.providers.serpapi_hotels import (
    _hotel_class_param,
    _no_results,
    _rating_code,
    normalize_property,
)

SPEC = SearchHotelsInput(
    city="Tokyo",
    area_name="Asakusa",
    check_in=date(2026, 10, 3),
    check_out=date(2026, 10, 8),
    adults=2,
)

PROPERTY = {
    "type": "hotel",
    "name": "Asakusa View Hotel",
    "link": "https://example.com/asakusa-view",
    "property_token": "ChkIzOnHwsTkxMTKARoLL2cvMXRkNTVfeXcQAQ",
    "gps_coordinates": {"latitude": 35.7148, "longitude": 139.7911},
    "check_in_time": "3:00 PM",
    "rate_per_night": {"lowest": "$286", "extracted_lowest": 286},
    "total_rate": {"lowest": "$1,430", "extracted_lowest": 1430},
    "prices": [
        {
            "source": "Booking.com",
            "link": "https://example.com/booking",
            "rate_per_night": {"lowest": "$296", "extracted_lowest": 296},
        },
        {
            "source": "Hotels.com",
            "link": "https://example.com/hotels",
            "rate_per_night": {"lowest": "$286", "extracted_lowest": 286},
            "total_rate": {"lowest": "$1,430", "extracted_lowest": 1430},
        },
    ],
    "hotel_class": "4-star hotel",
    "extracted_hotel_class": 4,
    "overall_rating": 4.3,
    "reviews": 2317,
    "location_rating": 4.6,
    "amenities": ["Free Wi-Fi", "Breakfast", "Air conditioning"],
}


def option(raw=None, *, live_mode=True):
    return normalize_property(raw or PROPERTY, SPEC, live_mode=live_mode)


# --- the two kinds of rating, kept apart -------------------------------------


def test_star_category_and_guest_rating_become_separate_ratings():
    ratings = {rating.type: rating for rating in option().ratings}

    assert ratings["star_category"].value == 4.0
    assert ratings["star_category"].review_count is None
    assert ratings["user_rating"].value == 4.3
    assert ratings["user_rating"].review_count == 2317
    assert ratings["star_category"].source == ratings["user_rating"].source == "google_hotels"


def test_the_location_rating_is_stored_but_is_not_the_location_score():
    """It is an opaque composite; the route-based score is the explainable one."""
    parsed = option()

    location = parsed.rating_of("location_score")
    assert location is not None and location.value == 4.6
    # Nothing has been routed yet, so there is no location figure to rank on.
    assert parsed.route_minutes == {}
    assert parsed.mean_route_minutes() is None


def test_a_property_with_no_reviews_gets_no_user_rating_at_all():
    raw = {key: value for key, value in PROPERTY.items() if key != "overall_rating"}

    parsed = option(raw)

    assert parsed.user_rating is None
    assert parsed.star_category.value == 4.0


# --- prices are per vendor ---------------------------------------------------


def test_every_vendor_quote_keeps_its_source():
    quotes = {quote.source: quote for quote in option().quotes}

    assert set(quotes) == {"Booking.com", "Hotels.com"}
    assert quotes["Booking.com"].nightly.amount == 296.0
    assert quotes["Hotels.com"].nightly.amount == 286.0
    assert quotes["Hotels.com"].total.amount == 1430.0


def test_the_cheapest_quote_is_identified_with_its_vendor():
    cheapest = option().cheapest_quote

    assert cheapest.source == "Hotels.com"
    assert cheapest.nightly.amount == 286.0


def test_a_property_with_no_headline_rate_falls_back_to_its_cheapest_quote():
    raw = {key: value for key, value in PROPERTY.items() if key != "rate_per_night"}

    parsed = option(raw)

    assert parsed.nightly_price.amount == 286.0


def test_a_property_with_no_price_anywhere_still_parses():
    raw = {
        key: value
        for key, value in PROPERTY.items()
        if key not in ("rate_per_night", "total_rate", "prices")
    }

    parsed = option(raw)

    assert parsed.nightly_price is None
    assert parsed.quotes == []


# --- the rest of the shape ---------------------------------------------------


def test_coordinates_and_identity_come_through():
    parsed = option()

    assert (parsed.lat, parsed.lng) == (35.7148, 139.7911)
    assert parsed.offer_ref == PROPERTY["property_token"]
    assert parsed.area_name == "Asakusa"
    assert parsed.amenities == ["Free Wi-Fi", "Breakfast", "Air conditioning"]


def test_a_property_with_no_name_is_dropped():
    nameless = {"rate_per_night": {"extracted_lowest": 100}}

    assert normalize_property(nameless, SPEC, live_mode=True) is None


def test_malformed_coordinates_do_not_invent_a_location():
    raw = {**PROPERTY, "gps_coordinates": {"latitude": "north-ish"}}

    parsed = option(raw)

    assert parsed.lat is None and parsed.lng is None


def test_the_live_mode_flag_is_threaded_onto_every_option():
    assert option(live_mode=False).live_mode is False
    assert option(live_mode=True).live_mode is True


def test_the_link_is_somewhere_to_look_not_somewhere_to_book():
    url = option().search_url

    assert url.startswith("https://www.google.com/travel/search")
    assert "Asakusa%20View%20Hotel" in url


# --- request parameters ------------------------------------------------------


def test_the_query_carries_the_city_as_well_as_the_area():
    """ "Shinjuku" alone is a gamble on the provider guessing the right country."""
    assert SPEC.query_text == "Asakusa Tokyo"


def test_a_rating_floor_maps_to_the_nearest_bucket_at_or_below_it():
    assert _rating_code(4.5) == "9"
    assert _rating_code(4.2) == "8"
    assert _rating_code(3.6) == "7"
    # Below the lowest bucket there is nothing to ask for; the service filters.
    assert _rating_code(2.0) is None
    assert _rating_code(None) is None


def test_a_star_floor_becomes_the_set_of_classes_at_or_above_it():
    """`hotel_class` is a set, not a floor: 4 alone would drop every 5-star."""
    assert _hotel_class_param(4) == "4,5"
    assert _hotel_class_param(3) == "3,4,5"
    assert _hotel_class_param(None) is None


# --- empty is not broken -----------------------------------------------------


def test_the_no_results_message_is_recognised_as_empty_rather_than_failed():
    assert _no_results("Google Hotels hasn't returned any results for this query.")
    assert not _no_results("Invalid API key. Your searches will not be processed.")


async def test_a_no_results_response_yields_no_hotels_and_no_error(monkeypatch):
    from app.providers import serpapi_hotels

    async def fake_request(*args, **kwargs):
        return {"error": "Google Hotels hasn't returned any results for this query."}

    monkeypatch.setattr(serpapi_hotels, "request_json", fake_request)
    provider = serpapi_hotels.SerpApiGoogleHotelsProvider("key", client=None)

    assert await provider.search_hotels(SPEC) == []


async def test_any_other_error_message_is_raised(monkeypatch):
    from app.providers import serpapi_hotels

    async def fake_request(*args, **kwargs):
        return {"error": "Invalid API key."}

    monkeypatch.setattr(serpapi_hotels, "request_json", fake_request)
    provider = serpapi_hotels.SerpApiGoogleHotelsProvider("key", client=None)

    with pytest.raises(ProviderBadRequest):
        await provider.search_hotels(SPEC)


async def test_paid_placements_are_not_recommendations(monkeypatch):
    """`ads` is bought; putting it in a ranking would make the ranking a lie."""
    from app.providers import serpapi_hotels

    async def fake_request(*args, **kwargs):
        return {
            "ads": [{**PROPERTY, "name": "Sponsored Tower"}],
            "properties": [PROPERTY],
        }

    monkeypatch.setattr(serpapi_hotels, "request_json", fake_request)
    provider = serpapi_hotels.SerpApiGoogleHotelsProvider("key", client=None)

    names = [option.name for option in await provider.search_hotels(SPEC)]

    assert names == ["Asakusa View Hotel"]


async def test_pagination_stops_once_the_limit_is_met(monkeypatch):
    from app.providers import serpapi_hotels

    pages = []

    async def fake_request(*args, params=None, **kwargs):
        pages.append(params.get("next_page_token"))
        return {
            "properties": [{**PROPERTY, "name": f"Hotel {len(pages)}-{i}"} for i in range(20)],
            "serpapi_pagination": {"next_page_token": f"token_{len(pages)}"},
        }

    monkeypatch.setattr(serpapi_hotels, "request_json", fake_request)
    provider = serpapi_hotels.SerpApiGoogleHotelsProvider("key", client=None)

    results = await provider.search_hotels(SPEC.model_copy(update={"limit": 5}))

    assert len(results) == 5
    assert pages == [None]


# --- the detail call, where the named booking sites live ---------------------

DETAIL = {
    "name": "Centurion Hotel Ueno",
    "address": "1-2-3 Ueno, Taito City, Tokyo",
    "prices": [
        {
            "source": "Centurion Hotel Ueno",
            "official": True,
            "link": "https://example.com/official",
            "rate_per_night": {"lowest": "$90", "extracted_lowest": 90},
            "total_rate": {"lowest": "$361", "extracted_lowest": 361},
            "free_cancellation": True,
        },
        {
            "source": "Agoda.com",
            "link": "https://example.com/agoda",
            "rate_per_night": {"lowest": "$97", "extracted_lowest": 97},
        },
    ],
    # Paid placement: aclk redirect with a gclid. Must never be read as a quote.
    "featured_prices": [
        {
            "source": "Expedia.com",
            "link": "https://www.google.com/aclk?sa=l&gclid=EAIaIQob",
            "rate_per_night": {"lowest": "$41", "extracted_lowest": 41},
        }
    ],
    "amenities": ["Free Wi-Fi", "Kid-friendly"],
}


def headline(amount: float = 70.0):
    parsed = option()
    parsed.headline_nightly = parsed.nightly_price = Money(amount=amount, currency="USD")
    parsed.quotes = []
    return parsed


async def test_the_detail_call_fills_in_the_named_booking_sites(monkeypatch):
    from app.providers import serpapi_hotels

    seen: list[dict] = []

    async def fake_request(*args, params=None, **kwargs):
        seen.append(params)
        return DETAIL

    monkeypatch.setattr(serpapi_hotels, "request_json", fake_request)
    provider = serpapi_hotels.SerpApiGoogleHotelsProvider("key", client=None)

    priced = headline()
    notes = await provider.fetch_quotes([priced], SPEC)

    assert seen[0]["property_token"] == priced.offer_ref
    assert [quote.source for quote in priced.quotes] == ["Centurion Hotel Ueno", "Agoda.com"]
    assert priced.cheapest_quote.nightly.amount == 90.0
    # Repriced to something a named site actually offers.
    assert priced.nightly_price.amount == 90.0
    assert priced.total_price.amount == 361.0
    assert priced.headline_nightly.amount == 70.0
    assert priced.refundable is True
    assert notes and "cheapest booking site listed is Centurion Hotel Ueno" in notes[0]


async def test_advertised_prices_are_never_read_as_quotes(monkeypatch):
    """`featured_prices` are aclk/gclid ads, and $41 is not a price on offer."""
    from app.providers import serpapi_hotels

    async def fake_request(*args, **kwargs):
        return DETAIL

    monkeypatch.setattr(serpapi_hotels, "request_json", fake_request)
    provider = serpapi_hotels.SerpApiGoogleHotelsProvider("key", client=None)

    priced = headline()
    await provider.fetch_quotes([priced], SPEC)

    assert "Expedia.com" not in {quote.source for quote in priced.quotes}
    assert priced.nightly_price.amount == 90.0


async def test_a_property_no_site_lists_is_reported_rather_than_repriced(monkeypatch):
    from app.providers import serpapi_hotels

    async def fake_request(*args, **kwargs):
        return {"name": "Somewhere", "prices": []}

    monkeypatch.setattr(serpapi_hotels, "request_json", fake_request)
    provider = serpapi_hotels.SerpApiGoogleHotelsProvider("key", client=None)

    priced = headline()
    notes = await provider.fetch_quotes([priced], SPEC)

    assert priced.quotes == []
    assert priced.nightly_price.amount == 70.0
    assert notes == ["Asakusa View Hotel: no booking site listed a price"]


async def test_a_headline_within_tolerance_raises_no_note(monkeypatch):
    from app.providers import serpapi_hotels

    async def fake_request(*args, **kwargs):
        return DETAIL

    monkeypatch.setattr(serpapi_hotels, "request_json", fake_request)
    provider = serpapi_hotels.SerpApiGoogleHotelsProvider("key", client=None)

    priced = headline(88.0)
    notes = await provider.fetch_quotes([priced], SPEC)

    assert priced.headline_gap() == 2.0
    assert notes == []


async def test_a_property_with_no_token_is_not_asked_about(monkeypatch):
    from app.providers import serpapi_hotels

    async def fake_request(*args, **kwargs):
        raise AssertionError("there is nothing to look up")

    monkeypatch.setattr(serpapi_hotels, "request_json", fake_request)
    provider = serpapi_hotels.SerpApiGoogleHotelsProvider("key", client=None)

    priced = headline()
    priced.offer_ref = None

    assert await provider.fetch_quotes([priced], SPEC) == []


# --- booking stays out of scope ----------------------------------------------


def test_the_provider_exposes_nothing_that_books():
    """Spec section 43, same rule the flight provider is held to."""
    import app.providers.serpapi_hotels as module
    from app.providers.serpapi_hotels import SerpApiGoogleHotelsProvider

    public = {
        name
        for name in dir(SerpApiGoogleHotelsProvider)
        if not name.startswith("_") and name not in ("name", "live_mode")
    }
    # Look up what is there, and look up what it costs. Nothing else.
    assert public == {"search_hotels", "fetch_quotes"}

    forbidden = ("order", "book", "pay", "reserve", "cancel", "checkout")
    for name in dir(module):
        if name.startswith("_"):
            continue
        assert not any(word in name.lower() for word in forbidden), name


def test_the_hotel_provider_interface_reads_and_never_writes():
    import app.providers.hotel_provider as module
    from app.providers.hotel_provider import HotelProvider

    public = {
        name
        for name in dir(HotelProvider)
        if not name.startswith("_") and name not in ("name", "live_mode")
    }
    assert public == {"search_hotels", "fetch_quotes"}
    assert "will ever gain a booking method" in (module.__doc__ or "")
