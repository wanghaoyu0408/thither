"""Caching, constrained by what Google's terms actually permit.

A blanket "store the response for 30 days" would breach the Maps Platform
terms, so cached values are classified by *content*, not by endpoint:

    PLACE_ID   exempt from ToS 3.2.3(b); storable indefinitely. Google
               recommends refreshing ids older than 12 months.
    LAT_LNG    temporarily cacheable for at most 30 consecutive days, after
               which the values must be deleted.
    VOLATILE   everything else - names, ratings, opening hours, reviews,
               addresses, route durations. Not persistable. In-process only,
               gone when the process exits.

`SqliteCache` raises on VOLATILE rather than quietly downgrading it. A policy
that is merely documented is a policy that erodes; one that throws is a policy
that holds.

Note the separation of concerns: this module exists to avoid API calls, which
is what the terms restrict. `TripState.entities` persisting a place's facts is
the user's own saved itinerary, governed instead by
`PlaceEntity.facts_updated_at` and the staleness rule in place_service.
"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.common import utcnow


class ContentClass(StrEnum):
    PLACE_ID = "place_id"
    LAT_LNG = "lat_lng"
    VOLATILE = "volatile"
    # Our own summaries of public pages, plus their URLs and titles. Not Google
    # Maps Content, so the terms constraining place data do not reach it - but
    # page text is still never stored, only what we wrote about it.
    RESEARCH = "research"
    # A climatology we computed from Open-Meteo reanalysis. Not Google Maps
    # Content, and stable by nature - what August did over the last fifteen
    # years does not change - so it is worth keeping rather than re-deriving
    # from a few hundred observations on every read.
    CLIMATE = "climate"


# Hard ceilings. A caller may ask for less, never for more.
MAX_TTL: dict[ContentClass, timedelta | None] = {
    ContentClass.PLACE_ID: None,  # no expiry
    ContentClass.LAT_LNG: timedelta(days=30),
    ContentClass.VOLATILE: timedelta(hours=1),
    ContentClass.RESEARCH: timedelta(days=14),
    ContentClass.CLIMATE: timedelta(days=180),
}

PERSISTABLE = frozenset(
    {ContentClass.PLACE_ID, ContentClass.LAT_LNG, ContentClass.RESEARCH, ContentClass.CLIMATE}
)


class CachePolicyError(RuntimeError):
    """Raised when a caller tries to store content the terms do not allow."""


@dataclass(frozen=True)
class CachePolicy:
    content_class: ContentClass
    ttl: timedelta | None = None

    def effective_ttl(self) -> timedelta | None:
        ceiling = MAX_TTL[self.content_class]
        if ceiling is None:
            return self.ttl
        return min(self.ttl, ceiling) if self.ttl else ceiling

    def expires_at(self, now: datetime | None = None) -> datetime | None:
        ttl = self.effective_ttl()
        return None if ttl is None else (now or utcnow()) + ttl

    @property
    def persistable(self) -> bool:
        return self.content_class in PERSISTABLE


PLACE_ID_POLICY = CachePolicy(ContentClass.PLACE_ID)
LAT_LNG_POLICY = CachePolicy(ContentClass.LAT_LNG)
VOLATILE_POLICY = CachePolicy(ContentClass.VOLATILE)
RESEARCH_POLICY = CachePolicy(ContentClass.RESEARCH)
CLIMATE_POLICY = CachePolicy(ContentClass.CLIMATE)


def naive_utc(value: datetime) -> datetime:
    """SQLite does not preserve tzinfo, so timestamps round-trip as naive.

    Comparing what comes back against an aware `utcnow()` raises TypeError, and
    the expiry check would blow up rather than expire anything. Everything the
    durable layer writes or compares is normalized to naive UTC.
    """
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def cache_key(namespace: str, payload: Any) -> str:
    """Stable key for a normalized request."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:{digest}"


class Cache(Protocol):
    async def get(self, key: str, policy: CachePolicy) -> Any | None: ...
    async def set(self, key: str, value: Any, policy: CachePolicy) -> None: ...


# How many entries the in-process layer will hold before evicting the oldest.
# It has a lifetime now (see `shared_memory`), and a TTL dict that only drops
# an entry when somebody asks for it again is a dict that never drops the keys
# nobody asks for. A route matrix payload is small; ten thousand of them is a
# few tens of megabytes, which is the right order for a cap that never bites in
# ordinary use and stops the leak in a long-lived process.
MAX_MEMORY_ENTRIES = 10_000


class InProcessCache:
    """TTL dict. Backs VOLATILE content, and it is the *only* layer allowed to.

    Bounded and insertion-ordered: on overflow the oldest entries go first,
    after a sweep for anything already expired. Nothing here is ever written to
    disk - that is the whole reason route durations and opening hours may be
    cached at all.
    """

    def __init__(self, max_entries: int = MAX_MEMORY_ENTRIES) -> None:
        self._entries: dict[str, tuple[datetime | None, Any]] = {}
        self._max_entries = max_entries

    async def get(self, key: str, policy: CachePolicy) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and expires_at <= utcnow():
            del self._entries[key]
            return None
        return value

    async def set(self, key: str, value: Any, policy: CachePolicy) -> None:
        self._entries[key] = (policy.expires_at(), value)
        if len(self._entries) > self._max_entries:
            self._evict()

    def _evict(self) -> None:
        """Expired first, then oldest, until back under the cap.

        Sweeping expiries first means a busy process reclaims lapsed entries
        rather than throwing away live ones it is about to need.
        """
        now = utcnow()
        for key in [
            key
            for key, (expires_at, _) in self._entries.items()
            if expires_at is not None and expires_at <= now
        ]:
            del self._entries[key]

        while len(self._entries) > self._max_entries:
            self._entries.pop(next(iter(self._entries)))

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


# One memory cache for the whole process.
#
# Every `Toolbox` used to build its own, and a Toolbox is built per request -
# six construction sites, one of them `_measure`, which runs on every button
# press. VOLATILE content is memory-only by policy, so route durations and
# opening hours were cached for exactly the length of one HTTP request and then
# thrown away: the durable table holds 1,210 lat/lng rows and not one route.
#
# Sharing it changes nothing the terms care about. It is still in-process,
# still capped at the same one-hour TTL, still gone when the process exits -
# the three properties this module's policy is actually about. Keys are content
# hashes of the normalized request, so two trips asking the same question share
# an answer, which is the point rather than a leak.
_SHARED_MEMORY = InProcessCache()


def shared_memory() -> InProcessCache:
    return _SHARED_MEMORY


def reset_shared_memory() -> None:
    """Tests only. A cache that outlives a test would make the next one lie."""
    _SHARED_MEMORY.clear()


class SqliteCache:
    """Durable cache for the two content classes that may legally persist."""

    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def get(self, key: str, policy: CachePolicy) -> Any | None:
        self._guard(policy)
        from app.db.models import ToolCacheRow

        async with self._sessions() as session:
            row = await session.get(ToolCacheRow, key)
            if row is None:
                return None
            if row.expires_at is not None and naive_utc(row.expires_at) <= naive_utc(utcnow()):
                await session.delete(row)
                await session.commit()
                return None
            return row.payload

    async def set(self, key: str, value: Any, policy: CachePolicy) -> None:
        self._guard(policy)
        from app.db.models import ToolCacheRow

        async with self._sessions() as session:
            row = await session.get(ToolCacheRow, key)
            if row is None:
                row = ToolCacheRow(key=key)
                session.add(row)
            expires_at = policy.expires_at()
            row.content_class = policy.content_class.value
            row.payload = value
            row.expires_at = None if expires_at is None else naive_utc(expires_at)
            await session.commit()

    async def purge_expired(self) -> int:
        """Delete lapsed rows. The 30-day lat/lng limit is a delete obligation,
        not merely a read-time filter."""
        from app.db.models import ToolCacheRow

        async with self._sessions() as session:
            result = await session.execute(
                delete(ToolCacheRow).where(
                    ToolCacheRow.expires_at.is_not(None),
                    ToolCacheRow.expires_at <= naive_utc(utcnow()),
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def count(self) -> int:
        from app.db.models import ToolCacheRow

        async with self._sessions() as session:
            rows = await session.execute(select(ToolCacheRow.key))
            return len(rows.scalars().all())

    @staticmethod
    def _guard(policy: CachePolicy) -> None:
        if not policy.persistable:
            raise CachePolicyError(
                f"{policy.content_class.value!r} content must not be written to durable "
                "storage; only place ids (indefinitely), lat/lng (<=30 days) and our own "
                "research summaries (<=14 days) may persist"
            )


class LayeredCache:
    """In-process in front of SQLite, falling back gracefully.

    VOLATILE never reaches the durable layer - it is not an error to cache it,
    only an error to persist it.
    """

    def __init__(self, memory: InProcessCache, durable: SqliteCache | None = None) -> None:
        self._memory = memory
        self._durable = durable

    async def get(self, key: str, policy: CachePolicy) -> Any | None:
        hit = await self._memory.get(key, policy)
        if hit is not None:
            return hit
        if self._durable is None or not policy.persistable:
            return None
        hit = await self._durable.get(key, policy)
        if hit is not None:
            await self._memory.set(key, hit, policy)
        return hit

    async def set(self, key: str, value: Any, policy: CachePolicy) -> None:
        await self._memory.set(key, value, policy)
        if self._durable is not None and policy.persistable:
            await self._durable.set(key, value, policy)


class RequestDeduper:
    """Collapses concurrent identical calls into one in-flight request.

    Unconditionally safe - nothing is stored - and the main practical saving:
    building an itinerary recomputes the same route matrix repeatedly inside a
    single run.
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future] = {}

    async def run(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        existing = self._inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future
        try:
            result = await factory()
        except BaseException as exc:
            future.set_exception(exc)
            # Nobody may be awaiting the future; keep the loop quiet about it.
            future.exception()
            raise
        else:
            future.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)
