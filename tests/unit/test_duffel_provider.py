"""Duffel normalization, offline.

Two shapes Duffel does not hand over ready-made: durations are ISO 8601 text,
and there is no stop count anywhere - it has to be derived.
"""

import pytest

from app.providers.duffel import (
    _search_url,
    is_test_token,
    normalize_offer,
    parse_duration,
)

# Shaped like a real offer request response, trimmed to what is read.
OFFER = {
    "id": "off_00009htYpSCXrwaB9DnUm0",
    "live_mode": True,
    "total_amount": "2568.40",
    "total_currency": "USD",
    "expires_at": "2026-10-01T12:30:00Z",
    "owner": {"iata_code": "UA", "name": "United Airlines"},
    "conditions": {
        "refund_before_departure": {"allowed": False},
        "change_before_departure": {"allowed": True},
    },
    "slices": [
        {
            "duration": "PT10H55M",
            "origin": {"iata_code": "SFO"},
            "destination": {"iata_code": "NRT"},
            "segments": [
                {
                    "origin": {"iata_code": "SFO"},
                    "destination": {"iata_code": "NRT"},
                    "departing_at": "2026-10-03T11:00:00",
                    "arriving_at": "2026-10-04T14:55:00",
                    "duration": "PT10H55M",
                    "marketing_carrier": {"iata_code": "UA", "name": "United Airlines"},
                    "operating_carrier": {"iata_code": "NH"},
                    "marketing_carrier_flight_number": "837",
                    "aircraft": {"name": "Boeing 777"},
                    "stops": [],
                    "passengers": [
                        {
                            "cabin_class": "economy",
                            "baggages": [
                                {"type": "checked", "quantity": 2},
                                {"type": "carry_on", "quantity": 1},
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}


def one_stop_offer() -> dict:
    return {
        **OFFER,
        "id": "off_stop",
        "slices": [
            {
                "duration": "PT15H20M",
                "origin": {"iata_code": "OAK"},
                "destination": {"iata_code": "NRT"},
                "segments": [
                    {
                        "origin": {"iata_code": "OAK"},
                        "destination": {"iata_code": "LAX"},
                        "departing_at": "2026-10-03T08:00:00",
                        "arriving_at": "2026-10-03T09:30:00",
                        "duration": "PT01H30M",
                        "marketing_carrier": {"iata_code": "AS"},
                        "stops": [],
                        "passengers": [{"cabin_class": "economy", "baggages": []}],
                    },
                    {
                        "origin": {"iata_code": "LAX"},
                        "destination": {"iata_code": "NRT"},
                        "departing_at": "2026-10-03T12:00:00",
                        "arriving_at": "2026-10-04T16:20:00",
                        "duration": "PT11H20M",
                        "marketing_carrier": {"iata_code": "AS"},
                        "stops": [],
                        "passengers": [{"cabin_class": "economy", "baggages": []}],
                    },
                ],
            }
        ],
    }


# --- durations ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PT10H55M", 655),
        ("PT02H26M", 146),
        ("PT45M", 45),
        ("PT2H", 120),
        ("P1DT3H30M", 1650),
        ("PT1H30M45S", 91),
        ("PT1H30M20S", 90),
        (None, None),
        ("", None),
        ("nonsense", None),
    ],
)
def test_iso_8601_durations_parse_to_minutes(raw, expected):
    assert parse_duration(raw) == expected


# --- tokens ------------------------------------------------------------------


def test_a_test_token_is_recognised():
    assert is_test_token("duffel_test_abc123") is True
    assert is_test_token("  duffel_test_abc  ") is True
    assert is_test_token("duffel_live_abc123") is False


# --- normalization -----------------------------------------------------------


def test_a_nonstop_offer_normalizes():
    option = normalize_offer(OFFER, live_mode=True, passengers=4, currency="USD")

    assert option.offer_ref == "off_00009htYpSCXrwaB9DnUm0"
    assert option.price.amount == 2568.40
    assert option.price_per_person.amount == 642.10
    assert option.origin == "SFO"
    assert option.destination == "NRT"
    assert option.stops == 0
    assert option.duration_minutes == 655
    assert option.airlines == ["UA"]
    assert option.cabin == "economy"
    assert option.live_mode is True


def test_stops_are_derived_from_segments():
    """Duffel reports no stop count; it is the connections plus tech stops."""
    option = normalize_offer(one_stop_offer(), live_mode=True, passengers=4, currency="USD")

    assert option.stops == 1
    assert option.connection_airports == ["LAX"]


def test_a_technical_stop_counts_toward_the_total():
    raw = one_stop_offer()
    raw["slices"][0]["segments"][1]["stops"] = [{"airport": {"iata_code": "ANC"}}]

    option = normalize_offer(raw, live_mode=True, passengers=1, currency="USD")

    assert option.stops == 2


def test_baggage_is_read_from_the_offer():
    option = normalize_offer(OFFER, live_mode=True, passengers=1, currency="USD")

    assert option.baggage.checked == 2
    assert option.baggage.cabin == 1


def test_absent_baggage_is_unknown_rather_than_zero():
    option = normalize_offer(one_stop_offer(), live_mode=True, passengers=1, currency="USD")

    assert option.baggage is None


def test_conditions_are_carried_through():
    option = normalize_offer(OFFER, live_mode=True, passengers=1, currency="USD")

    assert option.refundable is False
    assert option.changeable is True


def test_expiry_is_parsed():
    option = normalize_offer(OFFER, live_mode=True, passengers=1, currency="USD")

    assert option.expires_at is not None
    assert option.expires_at.year == 2026


def test_an_offer_with_no_usable_slice_is_dropped():
    assert (
        normalize_offer({"id": "off_x", "slices": []}, live_mode=True, passengers=1, currency="USD")
        is None
    )


def test_an_unparseable_price_drops_the_offer():
    raw = {**OFFER, "total_amount": "not a number"}

    assert normalize_offer(raw, live_mode=True, passengers=1, currency="USD") is None


# --- the sandbox flag --------------------------------------------------------


def test_a_test_token_makes_every_offer_sandbox():
    option = normalize_offer(OFFER, live_mode=False, passengers=1, currency="USD")

    assert option.live_mode is False


def test_an_offer_claiming_sandbox_wins_over_a_live_token():
    """If either says test data, it is test data."""
    raw = {**OFFER, "live_mode": False}

    option = normalize_offer(raw, live_mode=True, passengers=1, currency="USD")

    assert option.live_mode is False


# --- the link ----------------------------------------------------------------


def test_the_link_is_a_search_not_a_booking():
    url = _search_url("SFO", "NRT", "2026-10-03", "2026-10-08")

    assert url.startswith("https://www.google.com/travel/flights")
    assert "SFO.NRT.2026-10-03" in url


def test_a_one_way_link_omits_the_return():
    assert "*" not in _search_url("SFO", "NRT", "2026-10-03", None)


def test_the_provider_exposes_nothing_that_books():
    """Spec section 43. The way to keep booking out of scope is to have no code for it."""
    import app.providers.duffel as module
    from app.providers.duffel import DuffelProvider

    public = {name for name in dir(DuffelProvider) if not name.startswith("_")}
    assert public == {"search_offers"}

    forbidden = ("order", "book", "pay", "seat", "cancel")
    source = module.__doc__ or ""
    assert "never create an order" in source

    for name in dir(module):
        if name.startswith("_"):
            continue
        assert not any(word in name.lower() for word in forbidden), name
