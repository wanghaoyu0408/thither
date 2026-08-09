"""Google Hotels prices via SerpApi (spec section 16).

Search and price detail. Follows the `HotelProvider` interface and will never
gain a booking method.

Four properties of this API shape the code:

**The search is one call; per-vendor prices are one call each.** A search
returns twenty properties carrying a single headline `rate_per_night` and
usually no `prices[]` at all - on a live Ueno search, two of twenty had one.
The named booking sites live behind `property_token` in a per-property detail
call, so the shape is search-then-detail. Who pays for the details is the
caller's decision: `HotelService` spends them on the shortlist only.

**Both kinds of rating arrive together.** `overall_rating` with `reviews` is
what guests thought; `extracted_hotel_class` is the facility category. They
come from one response and are kept apart into two `HotelRating` entries,
because a four-star hotel and a 4.3-from-2,300-reviews are not the same claim.

**The headline rate is not a quote.** Google Hotels advertises a property "from
$70" while every booking site it lists wants $90. Nobody can be sent to pay the
advertised figure, so it is kept aside as `headline_nightly` and the price that
drives ranking is the cheapest one attributable to a named source.

**`featured_prices` are advertisements** - their links are `aclk` redirects
carrying a `gclid`. Only the organic `prices[]` array is read, for the same
reason the search's `ads` block is ignored: a purchased slot inside a
recommendation would make the recommendation a different thing than it claims.

`location_rating` is stored with its source but is never used as *the* location
score. It is an opaque composite; the route-based score in
`hotel_area_service` is a real travel time and can be quoted back with its
provenance.
"""

import asyncio
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from app.models.common import Money
from app.models.decision import HotelOptionData
from app.models.hotel import HotelPriceQuote, HotelRating, SearchHotelsInput
from app.providers.base import ProviderBadRequest, request_json

PROVIDER = "serpapi_google_hotels"
BASE_URL = "https://serpapi.com/search.json"
ENGINE = "google_hotels"

# One page is 20 properties. Three is more than a person can weigh, and each
# page is a billed request.
MAX_PAGES = 3

# Detail calls run in parallel, but not in a burst: a rate limit costs the whole
# shortlist, and five properties is not worth racing for.
MAX_CONCURRENT_DETAILS = 3

# A headline rate this far below every named booking site is worth saying out
# loud rather than leaving to be discovered at checkout.
HEADLINE_GAP_TOLERANCE = 5.0

# SerpApi answers "nothing matched" with HTTP 200 and an `error` string. That is
# a legitimately empty search, not a failed call, and conflating the two would
# have the agent retrying a query that will always be empty - or worse, treating
# a real outage as "no hotels here".
_NO_RESULTS_MARKERS = (
    "hasn't returned any results",
    "has not returned any results",
    "no results found",
)

# The provider takes a bucketed code rather than a number.
_RATING_CODES = ((4.5, "9"), (4.0, "8"), (3.5, "7"))


def _no_results(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _NO_RESULTS_MARKERS)


def _rating_code(minimum: float | None) -> str | None:
    """Highest bucket at or below what was asked, so nothing wanted is excluded."""
    if minimum is None:
        return None
    for threshold, code in _RATING_CODES:
        if minimum >= threshold:
            return code
    return None


def _hotel_class_param(minimum: float | None) -> str | None:
    """`hotel_class` is a set, not a floor: 4 alone would drop every 5-star."""
    if minimum is None:
        return None
    wanted = [str(star) for star in (2, 3, 4, 5) if star >= minimum]
    return ",".join(wanted) if wanted else None


def _money(value: Any, currency: str) -> Money | None:
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return Money(amount=round(amount, 2), currency=currency)


def _rate(block: Any, currency: str) -> Money | None:
    if not isinstance(block, dict):
        return None
    return _money(block.get("extracted_lowest"), currency)


def _ratings(raw: dict[str, Any]) -> list[HotelRating]:
    """Every rating the provider gave, each keeping what it measures.

    Never merged. A property with no reviews and a five-star category has one
    of these and not the other, and that difference has to survive into the
    ranking rather than being averaged away.
    """
    ratings: list[HotelRating] = []

    stars = raw.get("extracted_hotel_class")
    if isinstance(stars, int | float):
        ratings.append(
            HotelRating(
                value=float(stars), scale_max=5.0, type="star_category", source="google_hotels"
            )
        )

    overall = raw.get("overall_rating")
    if isinstance(overall, int | float):
        reviews = raw.get("reviews")
        ratings.append(
            HotelRating(
                value=float(overall),
                scale_max=5.0,
                type="user_rating",
                source="google_hotels",
                review_count=int(reviews) if isinstance(reviews, int | float) else None,
            )
        )

    location = raw.get("location_rating")
    if isinstance(location, int | float):
        # Kept for display. Not scored: nobody can say what went into it, and
        # the trip already has real travel times to the places it will visit.
        ratings.append(
            HotelRating(
                value=float(location),
                scale_max=5.0,
                type="location_score",
                source="google_hotels",
            )
        )

    return ratings


def _quotes(raw: dict[str, Any], currency: str) -> list[HotelPriceQuote]:
    """The organic `prices[]` array only.

    `featured_prices` is deliberately not read. Those entries are paid
    placements - `aclk` links carrying a `gclid` - and an advertisement inside a
    price comparison is not a price comparison.
    """
    quotes: list[HotelPriceQuote] = []
    for entry in raw.get("prices") or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not source:
            continue
        quotes.append(
            HotelPriceQuote(
                source=str(source),
                nightly=_rate(entry.get("rate_per_night"), currency),
                total=_rate(entry.get("total_rate"), currency),
                # The vendor's own page. Somewhere for a person to go and look;
                # nothing in this codebase follows it.
                link=entry.get("link"),
            )
        )
    return quotes


def _coordinates(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    gps = raw.get("gps_coordinates")
    if not isinstance(gps, dict):
        return None, None
    lat, lng = gps.get("latitude"), gps.get("longitude")
    if isinstance(lat, int | float) and isinstance(lng, int | float):
        return float(lat), float(lng)
    return None, None


def _search_url(name: str, check_in: date, check_out: date) -> str:
    query = f"{name} hotel {check_in.isoformat()} to {check_out.isoformat()}"
    return f"https://www.google.com/travel/search?q={quote(query)}"


def normalize_property(
    raw: dict[str, Any], spec: SearchHotelsInput, *, live_mode: bool
) -> HotelOptionData | None:
    """One SerpApi property in this application's shape, or None if unusable."""
    name = raw.get("name")
    if not name:
        return None

    lat, lng = _coordinates(raw)
    quotes = _quotes(raw, spec.currency)

    nightly = _rate(raw.get("rate_per_night"), spec.currency)
    total = _rate(raw.get("total_rate"), spec.currency)

    # A property with vendor quotes but no headline rate is common. Falling back
    # to the cheapest quote keeps it comparable instead of dropping it.
    if nightly is None and quotes:
        cheapest = min(
            (quote for quote in quotes if quote.nightly is not None),
            key=lambda quote: quote.nightly.amount,
            default=None,
        )
        nightly = cheapest.nightly if cheapest else None

    return HotelOptionData(
        provider=PROVIDER,
        offer_ref=raw.get("property_token"),
        live_mode=live_mode,
        name=str(name),
        lat=lat,
        lng=lng,
        area_name=spec.area_name,
        nightly_price=nightly,
        headline_nightly=_rate(raw.get("rate_per_night"), spec.currency),
        total_price=total,
        quotes=quotes,
        ratings=_ratings(raw),
        room_description=raw.get("description"),
        amenities=[str(item) for item in (raw.get("amenities") or []) if item],
        search_url=_search_url(str(name), spec.check_in, spec.check_out),
    )


def _apply_detail(
    option: HotelOptionData, payload: dict[str, Any], spec: SearchHotelsInput
) -> None:
    """Reprice and enrich one option from its detail response.

    The price that drives ranking becomes the cheapest quote that can be
    attributed to a named site. The advertised headline stays where it was, so
    the difference between "advertised" and "obtainable" survives rather than
    being resolved silently in either direction.
    """
    cheapest = option.cheapest_quote
    if cheapest and cheapest.nightly:
        option.nightly_price = cheapest.nightly
        if cheapest.total:
            option.total_price = cheapest.total

    # Free cancellation is a fact about a specific vendor's offer, so it counts
    # only when the cheapest one - the one being recommended - carries it.
    for entry in payload.get("prices") or []:
        if not isinstance(entry, dict):
            continue
        if cheapest and entry.get("source") == cheapest.source:
            option.refundable = bool(entry.get("free_cancellation")) or None
            break

    amenities = payload.get("amenities")
    if isinstance(amenities, list) and amenities:
        option.amenities = [str(item) for item in amenities if item][:12]


def _headline_note(option: HotelOptionData) -> str | None:
    gap = option.headline_gap()
    if gap is None or gap <= HEADLINE_GAP_TOLERANCE:
        return None

    cheapest = option.cheapest_quote
    return (
        f"{option.name}: advertised from "
        f"{option.headline_nightly.amount:.0f} {option.headline_nightly.currency}/night, but the "
        f"cheapest booking site listed is {cheapest.source} at "
        f"{cheapest.nightly.amount:.0f} {cheapest.nightly.currency}. The lower figure is not "
        f"attributable to anywhere you could book"
    )


class SerpApiGoogleHotelsProvider:
    """Hotel prices from Google Hotels, read through SerpApi.

    Deliberately has no method that could reserve or pay for anything.
    """

    name = PROVIDER

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._client = client
        # SerpApi has no sandbox: it scrapes the live Google Hotels result page.
        # A fake provider in the tests is what sets this False.
        self.live_mode = True

    def _params(self, spec: SearchHotelsInput) -> dict[str, Any]:
        params: dict[str, Any] = {
            "engine": ENGINE,
            "q": spec.query_text,
            "check_in_date": spec.check_in.isoformat(),
            "check_out_date": spec.check_out.isoformat(),
            "adults": spec.adults,
            "currency": spec.currency,
            "hl": "en",
            "api_key": self._api_key,
        }
        if spec.children:
            params["children"] = spec.children
            # Required alongside `children`; 8 is a neutral placeholder that
            # keeps room eligibility sane without inventing a specific child.
            params["children_ages"] = ",".join(["8"] * spec.children)
        if spec.max_nightly_price is not None:
            params["max_price"] = int(spec.max_nightly_price)

        rating = _rating_code(spec.min_rating)
        if rating:
            params["rating"] = rating

        hotel_class = _hotel_class_param(spec.min_star_category)
        if hotel_class:
            params["hotel_class"] = hotel_class

        return params

    async def search_hotels(self, spec: SearchHotelsInput) -> list[HotelOptionData]:
        """Properties for one area, following pagination up to `MAX_PAGES`."""
        params = self._params(spec)
        options: list[HotelOptionData] = []
        token: str | None = None

        for _page in range(MAX_PAGES):
            page_params = dict(params)
            if token:
                page_params["next_page_token"] = token

            payload = await request_json(
                self._client, "GET", BASE_URL, provider=PROVIDER, params=page_params
            )

            message = payload.get("error")
            if isinstance(message, str) and message:
                if _no_results(message):
                    break
                raise ProviderBadRequest(f"{PROVIDER}: {message}", PROVIDER)

            # `properties` only. The `ads` block is paid placement, and putting
            # a purchased slot into a recommendation would make the ranking a
            # different thing than it claims to be.
            for raw in payload.get("properties") or []:
                if not isinstance(raw, dict):
                    continue
                option = normalize_property(raw, spec, live_mode=self.live_mode)
                if option is not None:
                    options.append(option)

            if len(options) >= spec.limit:
                break

            token = (payload.get("serpapi_pagination") or {}).get("next_page_token")
            if not token:
                break

        return options[: spec.limit]

    async def fetch_quotes(
        self, options: list[HotelOptionData], spec: SearchHotelsInput
    ) -> list[str]:
        """Per-vendor prices for a handful of properties, one call each.

        Billed per property, so the caller decides which handful is worth it.
        Concurrency is bounded for the same reason the route matrix is: a burst
        of parallel searches is the fastest way to a rate limit.
        """
        wanted = [option for option in options if option.offer_ref]
        if not wanted:
            return []

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_DETAILS)

        async def one(option: HotelOptionData) -> str | None:
            params = {**self._params(spec), "property_token": option.offer_ref}
            async with semaphore:
                payload = await request_json(
                    self._client, "GET", BASE_URL, provider=PROVIDER, params=params
                )

            message = payload.get("error")
            if isinstance(message, str) and message:
                return f"{option.name}: no price detail available ({message})"

            option.quotes = _quotes(payload, spec.currency)
            if not option.quotes:
                return f"{option.name}: no booking site listed a price"

            _apply_detail(option, payload, spec)
            return _headline_note(option)

        notes = await asyncio.gather(*(one(option) for option in wanted))
        return [note for note in notes if note]
