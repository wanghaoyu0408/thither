"""Matrix chunking, waypoints and duration parsing.

The element cap is the load-bearing fact: 625 pairs for walking/driving, but
only 100 for transit, so a 12x12 transit matrix must be split.
"""

import pytest

from app.models.route import MAX_MATRIX_ELEMENTS, LocationRef
from app.providers.google_routes import _parse_duration, waypoint
from app.services.route_service import chunk_matrix, max_elements_for


def total_elements(chunks):
    return sum(len(origins) * len(destinations) for origins, destinations in chunks)


def covered_pairs(chunks):
    return {
        (origin, destination)
        for origins, destinations in chunks
        for origin in origins
        for destination in destinations
    }


def test_caps_come_from_the_mode():
    assert max_elements_for("transit") == 100
    assert max_elements_for("walking") == 625
    assert max_elements_for("driving") == 625
    assert MAX_MATRIX_ELEMENTS["TRANSIT"] == 100


def test_small_matrix_is_one_call():
    chunks = chunk_matrix(5, 5, 625)

    assert len(chunks) == 1
    assert total_elements(chunks) == 25


def test_twelve_by_twelve_transit_must_be_split():
    """144 elements against a 100-element cap - the case that fails in one call."""
    chunks = chunk_matrix(12, 12, 100)

    assert len(chunks) > 1
    assert all(len(o) * len(d) <= 100 for o, d in chunks)
    assert total_elements(chunks) == 144


@pytest.mark.parametrize(
    ("origins", "destinations", "cap"),
    [(12, 12, 100), (30, 30, 625), (1, 150, 100), (150, 1, 100), (7, 3, 100), (26, 26, 625)],
)
def test_chunks_stay_under_cap_and_cover_every_pair(origins, destinations, cap):
    chunks = chunk_matrix(origins, destinations, cap)

    assert all(len(o) * len(d) <= cap for o, d in chunks)
    assert covered_pairs(chunks) == {(o, d) for o in range(origins) for d in range(destinations)}
    # No pair computed twice - every element is billed.
    assert total_elements(chunks) == origins * destinations


def test_empty_matrix_makes_no_calls():
    assert chunk_matrix(0, 5, 100) == []
    assert chunk_matrix(5, 0, 100) == []


def test_destinations_wider_than_the_cap_are_split():
    chunks = chunk_matrix(1, 250, 100)

    assert len(chunks) == 3
    assert all(len(d) <= 100 for _, d in chunks)


# --- waypoints ---------------------------------------------------------------


def test_place_id_is_preferred_over_coordinates():
    ref = LocationRef(place_id="ChIJ_abc", lat=35.0, lng=139.0, address="somewhere")

    assert waypoint(ref) == {"placeId": "ChIJ_abc"}


def test_coordinates_used_when_no_place_id():
    assert waypoint(LocationRef(lat=35.66, lng=139.70)) == {
        "location": {"latLng": {"latitude": 35.66, "longitude": 139.70}}
    }


def test_address_is_the_last_resort():
    assert waypoint(LocationRef(address="Shibuya Station")) == {"address": "Shibuya Station"}


def test_a_reference_must_address_something():
    with pytest.raises(ValueError, match="needs one of"):
        LocationRef(label="just a name")


def test_lat_without_lng_is_not_an_address():
    with pytest.raises(ValueError, match="needs one of"):
        LocationRef(lat=35.0)


# --- durations ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1234s", 1234), ("0s", 0), ("1234.5s", 1234), (600, 600), (None, None), ("nonsense", None)],
)
def test_protobuf_durations_are_parsed(raw, expected):
    assert _parse_duration(raw) == expected
