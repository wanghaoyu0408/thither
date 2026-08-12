"""Cache policy, including the parts Google's terms actually constrain."""

import asyncio
from datetime import timedelta

import pytest

from app.models.common import utcnow
from app.services.cache import (
    LAT_LNG_POLICY,
    MAX_TTL,
    PLACE_ID_POLICY,
    VOLATILE_POLICY,
    CachePolicy,
    CachePolicyError,
    ContentClass,
    InProcessCache,
    LayeredCache,
    RequestDeduper,
    SqliteCache,
    cache_key,
)

# --- policy ------------------------------------------------------------------


def test_place_ids_never_expire():
    assert PLACE_ID_POLICY.effective_ttl() is None
    assert PLACE_ID_POLICY.expires_at() is None
    assert PLACE_ID_POLICY.persistable


def test_lat_lng_is_capped_at_thirty_days():
    assert LAT_LNG_POLICY.effective_ttl() == timedelta(days=30)
    assert MAX_TTL[ContentClass.LAT_LNG] == timedelta(days=30)


def test_a_caller_cannot_ask_for_longer_than_the_ceiling():
    greedy = CachePolicy(ContentClass.LAT_LNG, ttl=timedelta(days=365))

    assert greedy.effective_ttl() == timedelta(days=30)


def test_a_caller_may_ask_for_less():
    modest = CachePolicy(ContentClass.LAT_LNG, ttl=timedelta(days=1))

    assert modest.effective_ttl() == timedelta(days=1)


def test_volatile_content_is_not_persistable():
    assert VOLATILE_POLICY.persistable is False


def test_cache_keys_are_stable_and_order_independent():
    assert cache_key("ns", {"a": 1, "b": 2}) == cache_key("ns", {"b": 2, "a": 1})
    assert cache_key("ns", {"a": 1}) != cache_key("ns", {"a": 2})
    assert cache_key("one", {"a": 1}) != cache_key("two", {"a": 1})


# --- in-process --------------------------------------------------------------


async def test_in_process_round_trip():
    cache = InProcessCache()
    await cache.set("k", {"v": 1}, VOLATILE_POLICY)

    assert await cache.get("k", VOLATILE_POLICY) == {"v": 1}


async def test_in_process_entries_expire():
    cache = InProcessCache()
    await cache.set("k", "value", CachePolicy(ContentClass.VOLATILE, ttl=timedelta(seconds=-1)))

    assert await cache.get("k", VOLATILE_POLICY) is None


async def test_missing_key_is_none():
    assert await InProcessCache().get("absent", VOLATILE_POLICY) is None


# --- durable -----------------------------------------------------------------


async def test_sqlite_refuses_volatile_content(sessions):
    """The refusal is the whole point: names, ratings and hours must not persist."""
    cache = SqliteCache(sessions)

    with pytest.raises(CachePolicyError, match="must not be written to durable storage"):
        await cache.set("k", {"name": "Fuglen", "rating": 4.3}, VOLATILE_POLICY)

    with pytest.raises(CachePolicyError):
        await cache.get("k", VOLATILE_POLICY)


async def test_sqlite_stores_place_ids_without_expiry(sessions):
    cache = SqliteCache(sessions)
    await cache.set("pid", {"place_id": "ChIJ_abc"}, PLACE_ID_POLICY)

    assert await cache.get("pid", PLACE_ID_POLICY) == {"place_id": "ChIJ_abc"}


async def test_sqlite_stores_coordinates(sessions):
    cache = SqliteCache(sessions)
    await cache.set("ll", {"lat": 35.66, "lng": 139.70}, LAT_LNG_POLICY)

    assert (await cache.get("ll", LAT_LNG_POLICY))["lat"] == 35.66


async def test_lapsed_coordinates_are_not_served(sessions):
    cache = SqliteCache(sessions)
    await cache.set(
        "ll", {"lat": 1.0}, CachePolicy(ContentClass.LAT_LNG, ttl=timedelta(seconds=-1))
    )

    assert await cache.get("ll", LAT_LNG_POLICY) is None


async def test_purge_deletes_lapsed_rows(sessions):
    """The 30-day limit is a delete obligation, not just a read-time filter."""
    cache = SqliteCache(sessions)
    await cache.set("fresh", {"lat": 1.0}, LAT_LNG_POLICY)
    await cache.set("keep", {"place_id": "x"}, PLACE_ID_POLICY)
    await cache.set(
        "stale", {"lat": 2.0}, CachePolicy(ContentClass.LAT_LNG, ttl=timedelta(seconds=-1))
    )

    assert await cache.purge_expired() == 1
    assert await cache.count() == 2
    assert await cache.get("keep", PLACE_ID_POLICY) is not None


async def test_writing_the_same_key_twice_updates_in_place(sessions):
    cache = SqliteCache(sessions)
    await cache.set("k", {"lat": 1.0}, LAT_LNG_POLICY)
    await cache.set("k", {"lat": 2.0}, LAT_LNG_POLICY)

    assert (await cache.get("k", LAT_LNG_POLICY))["lat"] == 2.0
    assert await cache.count() == 1


# --- layered -----------------------------------------------------------------


async def test_layered_keeps_volatile_out_of_the_durable_layer(sessions):
    durable = SqliteCache(sessions)
    layered = LayeredCache(InProcessCache(), durable)

    # Must not raise: caching volatile content is fine, persisting it is not.
    await layered.set("k", {"rating": 4.3}, VOLATILE_POLICY)

    assert await layered.get("k", VOLATILE_POLICY) == {"rating": 4.3}
    assert await durable.count() == 0


async def test_layered_promotes_a_durable_hit_into_memory(sessions):
    durable = SqliteCache(sessions)
    memory = InProcessCache()
    await durable.set("k", {"lat": 9.9}, LAT_LNG_POLICY)

    layered = LayeredCache(memory, durable)

    assert (await layered.get("k", LAT_LNG_POLICY))["lat"] == 9.9
    assert (await memory.get("k", LAT_LNG_POLICY))["lat"] == 9.9


async def test_layered_works_without_a_durable_layer():
    layered = LayeredCache(InProcessCache(), None)
    await layered.set("k", {"lat": 1.0}, LAT_LNG_POLICY)

    assert await layered.get("k", LAT_LNG_POLICY) == {"lat": 1.0}


# --- dedupe ------------------------------------------------------------------


async def test_concurrent_identical_calls_share_one_request():
    deduper = RequestDeduper()
    calls = 0

    async def slow():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "result"

    results = await asyncio.gather(*(deduper.run("same", slow) for _ in range(5)))

    assert results == ["result"] * 5
    assert calls == 1


async def test_different_keys_are_not_deduped():
    deduper = RequestDeduper()
    calls = 0

    async def counted():
        nonlocal calls
        calls += 1
        return calls

    await asyncio.gather(deduper.run("a", counted), deduper.run("b", counted))

    assert calls == 2


async def test_sequential_calls_are_not_deduped():
    """Dedupe collapses concurrency; it is not a cache."""
    deduper = RequestDeduper()
    calls = 0

    async def counted():
        nonlocal calls
        calls += 1
        return calls

    await deduper.run("same", counted)
    await deduper.run("same", counted)

    assert calls == 2


async def test_a_failure_propagates_and_does_not_poison_the_key():
    deduper = RequestDeduper()

    async def boom():
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        await deduper.run("k", boom)

    async def fine():
        return "recovered"

    assert await deduper.run("k", fine) == "recovered"


async def test_expiry_uses_the_supplied_clock():
    policy = CachePolicy(ContentClass.LAT_LNG, ttl=timedelta(days=10))
    now = utcnow()

    assert policy.expires_at(now) == now + timedelta(days=10)


# --- the layer that has to survive a request ---------------------------------


async def test_volatile_content_survives_between_toolboxes():
    """The whole point of the shared memory layer.

    Route durations and opening hours are VOLATILE: they may never be written
    to disk, so the in-process cache is the only place they can live. A
    `Toolbox` is built per request - `_measure` runs on every button press -
    and each one used to bring its own empty `InProcessCache`, so a route
    measured for one click was gone by the next. The durable table proved it:
    1,210 lat/lng rows and not one route.
    """
    from app.services.cache import reset_shared_memory, shared_memory

    reset_shared_memory()
    first, second = shared_memory(), shared_memory()

    assert first is second, "one memory cache for the process, not one per caller"

    await first.set("routes:abc", {"minutes": 12.0}, VOLATILE_POLICY)
    assert await second.get("routes:abc", VOLATILE_POLICY) == {"minutes": 12.0}

    reset_shared_memory()
    assert await second.get("routes:abc", VOLATILE_POLICY) is None


async def test_the_shared_cache_is_bounded_so_a_long_process_cannot_leak():
    """A TTL dict that only drops an entry when somebody asks for it again is a
    dict that never drops the keys nobody asks for. That was harmless while it
    died with the request, and is a leak now that it does not."""
    cache = InProcessCache(max_entries=50)

    for index in range(200):
        await cache.set(f"k{index}", index, VOLATILE_POLICY)

    assert len(cache) <= 50
    # The newest survive; the oldest were the ones evicted.
    assert await cache.get("k199", VOLATILE_POLICY) == 199
    assert await cache.get("k0", VOLATILE_POLICY) is None


async def test_eviction_takes_the_expired_before_the_merely_old():
    """A busy process should reclaim lapsed entries rather than throw away live
    ones it is about to need."""
    cache = InProcessCache(max_entries=3)
    expired = CachePolicy(ContentClass.VOLATILE, ttl=timedelta(seconds=-1))

    await cache.set("stale_a", 1, expired)
    await cache.set("stale_b", 2, expired)
    await cache.set("live_a", 3, VOLATILE_POLICY)
    await cache.set("live_b", 4, VOLATILE_POLICY)

    assert await cache.get("live_a", VOLATILE_POLICY) == 3
    assert await cache.get("live_b", VOLATILE_POLICY) == 4
