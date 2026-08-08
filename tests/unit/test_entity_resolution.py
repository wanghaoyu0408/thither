"""Resolving provider results into registry entities."""

from datetime import timedelta

from app.models.common import utcnow
from app.models.place import PlaceSummary
from app.services.entity_service import (
    STALE_AFTER,
    is_stale,
    place_id_of,
    resolve_place,
    resolve_places,
    stale_entities,
)
from tests.conftest import make_entity


def summary(place_id="ChIJ_fuglen", **overrides) -> PlaceSummary:
    base = {
        "place_id": place_id,
        "name": "Fuglen Tokyo",
        "address": "1-16-11 Tomigaya, Shibuya City",
        "lat": 35.6659,
        "lng": 139.6979,
        "categories": ["cafe"],
        "rating": 4.3,
        "rating_count": 2317,
    }
    return PlaceSummary(**{**base, **overrides})


def test_a_new_place_gets_a_new_entity():
    entity = resolve_place(summary(), {})

    assert entity.entity_id.startswith("ent_")
    assert place_id_of(entity) == "ChIJ_fuglen"
    assert entity.name == "Fuglen Tokyo"
    assert entity.rating == 4.3


def test_a_known_place_keeps_its_entity_id():
    """Re-running discovery must not fork the registry."""
    existing = make_entity("ent_cafe", "Fuglen Tokyo")
    existing.provider_refs = {"google_place_id": "ChIJ_fuglen"}

    entity = resolve_place(summary(), {"ent_cafe": existing})

    assert entity.entity_id == "ent_cafe"


def test_resolution_is_idempotent_across_runs():
    first = resolve_places([summary()], {})
    registry = {entity.entity_id: entity for entity in first}

    second = resolve_places([summary()], registry)

    assert [e.entity_id for e in second] == [e.entity_id for e in first]


def test_duplicates_within_one_batch_collapse():
    """Without a running merge, two summaries for one place mint two ids."""
    resolved = resolve_places([summary(), summary()], {})

    assert len({entity.entity_id for entity in resolved}) == 1


def test_distinct_places_stay_distinct():
    resolved = resolve_places([summary(), summary("ChIJ_other", name="Somewhere Else")], {})

    assert len({entity.entity_id for entity in resolved}) == 2


def test_a_thin_search_result_does_not_erase_richer_stored_facts():
    """A BASIC search after a FULL details fetch must not wipe the hours."""
    stored = resolve_place(
        summary(opening_hours={"openNow": True}, website_url="https://fuglen.com"), {}
    )
    registry = {stored.entity_id: stored}

    thin = PlaceSummary(place_id="ChIJ_fuglen", name="Fuglen Tokyo")
    merged = resolve_place(thin, registry)

    assert merged.opening_hours == {"openNow": True}
    assert merged.website_url == "https://fuglen.com"
    assert merged.rating == 4.3


def test_newer_facts_do_overwrite():
    stored = resolve_place(summary(rating=4.3), {})
    registry = {stored.entity_id: stored}

    merged = resolve_place(summary(rating=4.6, rating_count=2400), registry)

    assert merged.rating == 4.6
    assert merged.rating_count == 2400


def test_provider_refs_are_merged_not_replaced():
    stored = make_entity("ent_cafe", "Fuglen Tokyo")
    stored.provider_refs = {"google_place_id": "ChIJ_fuglen", "some_other": "abc"}

    merged = resolve_place(summary(), {"ent_cafe": stored})

    assert merged.provider_refs["some_other"] == "abc"
    assert merged.provider_refs["google_place_id"] == "ChIJ_fuglen"


# --- staleness ---------------------------------------------------------------


def test_fresh_facts_are_not_stale():
    assert is_stale(resolve_place(summary(), {})) is False


def test_facts_older_than_the_window_are_stale():
    entity = resolve_place(summary(), {})
    entity.facts_updated_at = utcnow() - STALE_AFTER - timedelta(days=1)

    assert is_stale(entity) is True


def test_stale_entities_are_listed_for_refresh():
    fresh = resolve_place(summary(), {})
    old = resolve_place(summary("ChIJ_old", name="Old Place"), {})
    old.facts_updated_at = utcnow() - timedelta(days=90)

    listed = stale_entities({fresh.entity_id: fresh, old.entity_id: old})

    assert [entity.entity_id for entity in listed] == [old.entity_id]


def test_naive_timestamps_do_not_crash_the_comparison():
    entity = resolve_place(summary(), {})
    entity.facts_updated_at = entity.facts_updated_at.replace(tzinfo=None)

    assert is_stale(entity) is False
