"""Business-level flight search (spec section 15).

A Duffel slice holds one origin and one destination, so "compare SFO, SJC and
OAK" fans out into one offer request per pair, run concurrently and merged -
structurally the same problem as M2's route-matrix chunking, and it needs the
same two guards: bounded concurrency and a cap on how many pairs a single
request may spend.

Two things this layer refuses to do:

- **Persist prices.** Offers carry an `expires_at` measured in minutes. They go
  in the in-process cache only; `ContentClass.VOLATILE` already refuses durable
  storage, so no new class is needed.
- **Return an expired offer.** An offer past its expiry is not a cheap flight,
  it is a dead id. Dropped, and counted in the warnings.
"""

import asyncio
from datetime import timedelta

from app.models.common import utcnow
from app.models.decision import FlightOptionData
from app.models.flight import SearchFlightsInput
from app.models.tool import ToolResult
from app.providers.base import ProviderError
from app.providers.duffel import PROVIDER, DuffelProvider
from app.services.cache import VOLATILE_POLICY, Cache, RequestDeduper, cache_key

MAX_CONCURRENT_SEARCHES = 4
# Origins x destinations. Three Bay Area airports against two Tokyo ones is six,
# which is the realistic ceiling for a comparison a person can act on.
MAX_PAIRS = 6
OFFER_TTL = timedelta(minutes=10)

SANDBOX_DISCLAIMER = (
    "SANDBOX DATA: these offers come from the provider's test environment. The "
    "flights, times and prices are not real and must not be presented to the user "
    "as real. Say so explicitly if you mention them at all."
)


class FlightService:
    def __init__(
        self,
        provider: DuffelProvider,
        cache: Cache,
        deduper: RequestDeduper | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._deduper = deduper or RequestDeduper()

    @property
    def live_mode(self) -> bool:
        return self._provider.live_mode

    async def search_flights(self, spec: SearchFlightsInput) -> ToolResult[FlightOptionData]:
        warnings: list[str] = []

        pairs = [
            (origin.strip().upper(), destination.strip().upper())
            for origin in spec.origins
            for destination in spec.destinations
        ]
        if len(pairs) > MAX_PAIRS:
            warnings.append(
                f"{len(pairs)} origin/destination pairs requested; searching the first "
                f"{MAX_PAIRS}. Narrow the airports to compare the rest."
            )
            pairs = pairs[:MAX_PAIRS]

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
        results = await asyncio.gather(
            *(
                self._one_pair(origin, destination, spec, semaphore)
                for origin, destination in pairs
            ),
            return_exceptions=False,
        )

        offers: list[FlightOptionData] = []
        failures: dict[str, ProviderError] = {}
        for (origin, destination), outcome in zip(pairs, results, strict=True):
            if isinstance(outcome, ProviderError):
                failures[f"{origin}-{destination}"] = outcome
                continue
            offers.extend(outcome)

        for route, failure in failures.items():
            warnings.append(f"{route} search failed: {failure.message}")

        if failures and not offers:
            # Every route failed. Distinct from every route being empty - and
            # the *kind* of failure is carried through rather than flattened.
            # An earlier version hardcoded provider_unavailable/retryable here,
            # which reported a permissions problem as a passing outage and would
            # have had the agent retrying something that could never succeed.
            error = next(iter(failures.values())).as_tool_error()
            error.message = f"no route could be searched; {error.message}"
            return ToolResult[FlightOptionData](source=PROVIDER, warnings=warnings, error=error)

        offers, dropped = _drop_expired(offers)
        if dropped:
            warnings.append(f"{dropped} offer(s) had already expired and were discarded")

        offers = _apply_filters(offers, spec, warnings)

        # Which airports actually have service matters as much as the fares.
        # Searching SFO, OAK and SJC to Tokyo returns SFO only - and saying so
        # is the answer to "should we fly from Oakland?", where showing SFO
        # results without comment is not.
        warnings.extend(_report_by_origin(offers, pairs, failures))

        offers, duplicates = _dedupe(offers)
        if duplicates:
            warnings.append(
                f"{duplicates} near-identical offer(s) collapsed (same flight, price and "
                "conditions under different fare names)"
            )

        offers.sort(key=lambda offer: (offer.price.amount, offer.offer_ref))
        offers = offers[: spec.limit]

        if not self.live_mode and offers:
            warnings.append(SANDBOX_DISCLAIMER)

        return ToolResult[FlightOptionData](
            source=PROVIDER,
            results=offers,
            warnings=warnings,
            expires_at=min((o.expires_at for o in offers if o.expires_at), default=None),
        )

    async def _one_pair(
        self,
        origin: str,
        destination: str,
        spec: SearchFlightsInput,
        semaphore: asyncio.Semaphore,
    ) -> list[FlightOptionData] | ProviderError:
        key = cache_key(
            "flights:offers",
            {
                "o": origin,
                "d": destination,
                "out": spec.departure_date.isoformat(),
                "back": spec.return_date.isoformat() if spec.return_date else None,
                "adults": spec.adults,
                "children": spec.children,
                "cabin": spec.cabin,
                "max_stops": spec.max_stops,
            },
        )

        cached = await self._cache.get(key, VOLATILE_POLICY)
        if cached is not None:
            return [FlightOptionData.model_validate(row) for row in cached]

        async def call() -> list[FlightOptionData]:
            async with semaphore:
                return await self._provider.search_offers(
                    origin=origin, destination=destination, spec=spec
                )

        try:
            offers = await self._deduper.run(key, call)
        except ProviderError as exc:
            return exc

        await self._cache.set(
            key,
            [offer.model_dump(mode="json") for offer in offers],
            VOLATILE_POLICY,
        )
        return offers


def _report_by_origin(
    offers: list[FlightOptionData],
    pairs: list[tuple[str, str]],
    failures: dict[str, ProviderError],
) -> list[str]:
    """Say what each origin produced, including the ones that produced nothing."""
    counts: dict[str, int] = {}
    for offer in offers:
        counts[offer.origin] = counts.get(offer.origin, 0) + 1

    notes: list[str] = []
    for origin in dict.fromkeys(origin for origin, _destination in pairs):
        if any(route.startswith(f"{origin}-") for route in failures):
            continue
        if not counts.get(origin):
            notes.append(f"{origin}: no airline offered this route on these dates")
    return notes


def _dedupe(offers: list[FlightOptionData]) -> tuple[list[FlightOptionData], int]:
    """Collapse offers that differ only in fare name.

    Airlines return the same flight several times under different branding. Five
    identical rows is not a shortlist (spec section 33), but the signature keeps
    baggage and flexibility in it, so genuinely different fares survive.
    """
    seen: dict[tuple, FlightOptionData] = {}
    for offer in offers:
        signature = (
            offer.origin,
            offer.destination,
            round(offer.price.amount, 2),
            offer.stops,
            offer.departure_at,
            tuple(offer.airlines),
            offer.baggage.checked if offer.baggage else None,
            offer.refundable,
            offer.changeable,
        )
        seen.setdefault(signature, offer)
    return list(seen.values()), len(offers) - len(seen)


def _drop_expired(offers: list[FlightOptionData]) -> tuple[list[FlightOptionData], int]:
    now = utcnow()
    alive = [offer for offer in offers if not offer.is_expired(now)]
    return alive, len(offers) - len(alive)


def _apply_filters(
    offers: list[FlightOptionData], spec: SearchFlightsInput, warnings: list[str]
) -> list[FlightOptionData]:
    """Filters the provider could not apply server-side.

    Each one reports how many it removed, so an empty result is explained rather
    than mysterious.
    """
    kept = offers

    if spec.max_stops is not None:
        before = len(kept)
        kept = [offer for offer in kept if (offer.stops or 0) <= spec.max_stops]
        if before != len(kept):
            warnings.append(f"{before - len(kept)} offer(s) had more than {spec.max_stops} stop(s)")

    if spec.max_price_per_person is not None:
        before = len(kept)
        kept = [
            offer
            for offer in kept
            if offer.price_per_person is None
            or offer.price_per_person.amount <= spec.max_price_per_person
        ]
        if before != len(kept):
            warnings.append(
                f"{before - len(kept)} offer(s) were over "
                f"{spec.max_price_per_person:.0f} per person"
            )

    if spec.earliest_departure or spec.latest_departure:
        before = len(kept)
        kept = [offer for offer in kept if _departs_in_window(offer, spec)]
        if before != len(kept):
            warnings.append(f"{before - len(kept)} offer(s) departed outside the requested window")

    return kept


def _departs_in_window(offer: FlightOptionData, spec: SearchFlightsInput) -> bool:
    if offer.departure_at is None:
        return True
    clock = offer.departure_at.strftime("%H:%M")
    if spec.earliest_departure and clock < spec.earliest_departure:
        return False
    return not (spec.latest_departure and clock > spec.latest_departure)
