"""Business-level hotel search (spec sections 16 and 25).

Three rules are enforced here rather than asked for in a prompt, because M3 and
M5 both taught that guidance the model can forget is not a rule:

**The area comes first.** `search_hotels` refuses to run without a resolved
neighbourhood and names the tool that produces one. Searching a whole city for
hotels is the step spec section 25 explicitly puts *after* choosing where to
stay. Skipping it stays possible - "book the Park Hyatt" is a legitimate
request - but only through `bypass_area_decision`, which requires a reason and
stores it on the option.

**Prices are never persisted.** Nightly rates change hourly. They live in the
in-process cache only, which `ContentClass.VOLATILE` already refuses to write
to disk - the same rule M5 applies to fares, and the right one whichever
provider's terms are in play.

**Enrichment is spent on the shortlist only.** Resolving twenty hotels to
Google places and routing all of them to every anchor would be twenty searches
and a large matrix to answer a question about five. `shortlist()` ranks first
and enriches after, so the ordering is structural rather than remembered.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from app.models.common import utcnow
from app.models.decision import (
    Decision,
    DecisionOption,
    HotelAreaOption,
    HotelOptionData,
)
from app.models.entity import PlaceEntity
from app.models.hotel import HotelRating, SearchHotelsInput
from app.models.place import PlaceFieldSet, PlaceSummary, SearchPlacesInput
from app.models.route import LocationRef, TravelMode
from app.models.tool import ToolError, ToolResult
from app.models.traveler import HotelPreferences
from app.models.trip import TripState
from app.providers.base import ProviderError
from app.providers.hotel_provider import HotelProvider
from app.services.cache import VOLATILE_POLICY, Cache, RequestDeduper, cache_key
from app.services.entity_service import place_id_of, resolve_place
from app.services.hotel_area_service import anchor_entities, departure_time_for, measure_travel
from app.services.hotel_ranking import RankedHotel, rank_hotels, unscored_preferences
from app.services.place_service import PlaceService
from app.services.route_service import RouteService

QUOTE_TTL = timedelta(minutes=30)

# How many hotels are worth the enrichment calls. Spec section 33: a shortlist
# a person can hold in their head, not a catalogue.
DEFAULT_SHORTLIST = 5

# How far from a hotel's own coordinates a Places match may sit before it is
# probably a different building.
MATCH_RADIUS_METERS = 500

# Two Google user ratings for the same hotel differing by less than this are the
# same number twice, and printing both would be noise dressed as corroboration.
RATING_AGREEMENT = 0.1

# Words that identify nothing when matching a hotel name.
_NOISE_WORDS = frozenset(
    {"hotel", "hotels", "inn", "the", "and", "resort", "suites", "tokyo", "by", "at"}
)

AREA_REQUIRED = (
    "no hotel area is set for this trip. Choosing a neighbourhood comes before choosing a "
    "hotel: call recommend_hotel_areas first, or pass bypass_area_decision with a reason if "
    "the traveller has already named a specific hotel."
)

SANDBOX_DISCLAIMER = (
    "SANDBOX DATA: these hotels come from the provider's test environment. The properties, "
    "prices and availability are not real and must not be presented to the user as real. Say "
    "so explicitly if you mention them at all."
)


@dataclass
class Shortlist:
    """Ranked hotels plus what the caller must patch into the trip.

    The entities are returned rather than written: every mutation of TripState
    goes through the patch pipeline, and a service reaching around it would
    bypass locks, rejections and the revision check.
    """

    ranked: list[RankedHotel] = field(default_factory=list)
    entities: list[PlaceEntity] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def selected_area(state: TripState) -> HotelAreaOption | None:
    """The neighbourhood this trip has actually settled on, if any."""
    decision = state.decisions.hotel_area
    if decision is None or not decision.selected_option_id:
        return None
    return next(
        (
            option.data
            for option in decision.options
            if option.option_id == decision.selected_option_id
        ),
        None,
    )


def resolve_area(
    spec: SearchHotelsInput, state: TripState | None
) -> tuple[SearchHotelsInput, str | None]:
    """Fill in the area from the trip, or explain why the search cannot run.

    Returns the spec to search with and, when the ordering was not satisfied,
    the message saying so. An explicit `area_name` counts as resolved: a
    traveller naming a neighbourhood has made the decision the tool exists to
    support.
    """
    city = spec.city or (state.brief.destination.city if state else None)
    updates: dict[str, object] = {"city": city}

    if spec.bypass_area_decision or spec.area_name:
        return spec.model_copy(update=updates), None

    area = selected_area(state) if state else None
    if area is None:
        return spec, AREA_REQUIRED

    updates["area_name"] = area.area_name
    if area.center is not None:
        updates["area_lat"] = area.center.lat
        updates["area_lng"] = area.center.lng

    return spec.model_copy(update=updates), None


def _significant(name: str) -> set[str]:
    return {
        word
        for word in "".join(c if c.isalnum() else " " for c in name.lower()).split()
        if len(word) >= 4 and word not in _NOISE_WORDS
    }


def looks_like(hotel_name: str, place_name: str | None) -> bool:
    """Whether a Places result is plausibly the same property.

    Proximity alone is not enough: within 500 metres of a Tokyo hotel there are
    several other hotels, and attaching the wrong entity would put the wrong
    rating and the wrong address on the recommendation. Same discipline M3
    learned about labelling days by address rather than by name.
    """
    if not place_name:
        return False
    wanted = _significant(hotel_name)
    if not wanted:
        # Nothing distinctive to match on; refuse rather than guess.
        return False
    return bool(wanted & _significant(place_name))


class HotelService:
    def __init__(
        self,
        provider: HotelProvider,
        places: PlaceService,
        routes: RouteService,
        cache: Cache,
        deduper: RequestDeduper | None = None,
    ) -> None:
        self._provider = provider
        self._places = places
        self._routes = routes
        self._cache = cache
        self._deduper = deduper or RequestDeduper()

    @property
    def live_mode(self) -> bool:
        return self._provider.live_mode

    async def search_hotels(
        self,
        spec: SearchHotelsInput,
        *,
        state: TripState | None = None,
    ) -> ToolResult[HotelOptionData]:
        source = self._provider.name

        spec, problem = resolve_area(spec, state)
        if problem:
            # A refusal, not an empty result. The difference matters: "there are
            # no hotels" and "you have not chosen where to stay yet" call for
            # completely different next moves.
            return ToolResult[HotelOptionData](
                source=source,
                error=ToolError(code="invalid_request", message=problem, provider=source),
            )

        warnings: list[str] = []
        if spec.bypass_area_decision:
            warnings.append(
                f"the neighbourhood step was skipped: {spec.bypass_reason}. This search covers "
                f"{spec.query_text} as a whole rather than a chosen area."
            )

        key = cache_key(
            "hotels:search",
            {
                "provider": source,
                "q": spec.query_text,
                "in": spec.check_in.isoformat(),
                "out": spec.check_out.isoformat(),
                "adults": spec.adults,
                "children": spec.children,
                "rooms": spec.rooms,
                "currency": spec.currency,
                "max_nightly": spec.max_nightly_price,
                "min_rating": spec.min_rating,
                "min_star": spec.min_star_category,
                "limit": spec.limit,
            },
        )

        cached = await self._cache.get(key, VOLATILE_POLICY)
        if cached is not None:
            options = [HotelOptionData.model_validate(row) for row in cached]
        else:
            try:
                options = await self._deduper.run(key, lambda: self._provider.search_hotels(spec))
            except ProviderError as exc:
                return ToolResult[HotelOptionData](
                    source=source, warnings=warnings, error=exc.as_tool_error()
                )

            await self._cache.set(
                key, [option.model_dump(mode="json") for option in options], VOLATILE_POLICY
            )

        if spec.bypass_area_decision and spec.bypass_reason:
            # Stored on the option so the trace survives into TripState rather
            # than living only in this call's warnings.
            options = [
                option.model_copy(update={"area_bypass_reason": spec.bypass_reason})
                for option in options
            ]

        options = _apply_filters(options, spec, warnings)
        options.sort(key=lambda option: (_sort_price(option), option.name))

        if not self.live_mode and options:
            warnings.append(SANDBOX_DISCLAIMER)

        return ToolResult[HotelOptionData](
            source=source,
            results=options,
            warnings=warnings,
            expires_at=None if not options else _expiry(options),
        )

    async def shortlist(
        self,
        options: list[HotelOptionData],
        *,
        state: TripState,
        preferences: HotelPreferences | None = None,
        size: int = DEFAULT_SHORTLIST,
        mode: TravelMode = "transit",
    ) -> Shortlist:
        """Rank, cut to a shortlist, then spend the enrichment calls on it.

        The order is the point. Ranking first uses only what the hotel provider
        already returned; Google Places and Routes are then asked about the few
        that survived. Doing it the other way round costs twenty searches and a
        matrix to answer a question about five.
        """
        preferences = preferences or HotelPreferences()
        warnings = unscored_preferences(preferences)

        if not options:
            return Shortlist(warnings=warnings)

        first_pass = rank_hotels(options, preferences=preferences)
        cheapest = min(
            (item.nightly for item in first_pass if item.nightly is not None), default=None
        )

        top = [item.option for item in first_pass[:size]]

        entities, enrich_warnings = await self._enrich(top, state)
        warnings.extend(enrich_warnings)

        route_warnings = await self._add_route_minutes(top, state, entities, mode=mode)
        warnings.extend(route_warnings)

        return Shortlist(
            ranked=rank_hotels(top, preferences=preferences, cheapest=cheapest),
            entities=entities,
            warnings=warnings,
        )

    async def _enrich(
        self, options: list[HotelOptionData], state: TripState
    ) -> tuple[list[PlaceEntity], list[str]]:
        """Resolve shortlisted hotels to Google places.

        Mutates each option in place with the entity id and any rating Places
        adds. The entities come back for the caller to patch in - nothing here
        writes to TripState.
        """
        warnings: list[str] = []
        registry = dict(state.entities)
        entities: list[PlaceEntity] = []

        for option in options:
            lat = option.lat
            lng = option.lng
            if lat is None or lng is None:
                warnings.append(f"{option.name}: no coordinates, so it could not be verified")
                continue

            result = await self._places.search_places(
                SearchPlacesInput(
                    query=option.name,
                    lat=lat,
                    lng=lng,
                    radius_meters=MATCH_RADIUS_METERS,
                    limit=3,
                    field_set=PlaceFieldSet.RANKING,
                )
            )
            if not result.ok:
                warnings.append(f"{option.name}: Places lookup failed, {result.error.message}")
                continue

            match = next(
                (place for place in result.results if looks_like(option.name, place.name)), None
            )
            if match is None:
                warnings.append(
                    f"{option.name}: no Google place matched this name nearby, so its rating "
                    "and address are the hotel provider's alone"
                )
                continue

            entity = resolve_place(match, registry)
            registry[entity.entity_id] = entity
            entities.append(entity)

            option.entity_id = entity.entity_id
            if entity.lat and entity.lng:
                option.lat, option.lng = entity.lat, entity.lng

            _merge_places_rating(option, match)

        return entities, warnings

    async def _add_route_minutes(
        self,
        options: list[HotelOptionData],
        state: TripState,
        entities: list[PlaceEntity],
        *,
        mode: TravelMode,
    ) -> list[str]:
        """Real travel minutes from each shortlisted hotel to each anchor.

        The figure the location dimension is built on. Without it that dimension
        stays `None` and the ranking says so, rather than falling back to a
        straight line (spec section 21).
        """
        anchors = anchor_entities(state)
        if not anchors:
            return [
                "no scheduled or shortlisted places yet, so hotel travel times were not measured"
            ]

        placed = [option for option in options if option.lat is not None and option.lng is not None]
        if not placed:
            return ["no hotel had coordinates, so travel times could not be measured"]

        by_id = {entity.entity_id: entity for entity in entities}
        origins: list[LocationRef] = []
        for option in placed:
            entity = by_id.get(option.entity_id or "")
            origins.append(
                LocationRef(
                    place_id=place_id_of(entity) if entity else None,
                    lat=option.lat,
                    lng=option.lng,
                    label=option.name,
                )
            )

        destinations = [
            LocationRef(entity_id=entity.entity_id, label=entity.name) for entity in anchors
        ]

        used, result, warnings = await measure_travel(
            self._routes,
            origins=origins,
            destinations=destinations,
            entities=state.entities,
            mode=mode,
            departure_at=departure_time_for(state),
        )
        if not result.ok:
            return [f"hotel travel times unavailable: {result.error.message}"]

        for leg in result.results:
            if leg.status != "ok" or leg.duration_minutes is None:
                continue
            anchor = anchors[leg.destination_index]
            hotel = placed[leg.origin_index]
            hotel.route_minutes[anchor.entity_id] = round(leg.duration_minutes, 1)
            hotel.route_mode = used

        return [*warnings, *result.warnings]


def _merge_places_rating(option: HotelOptionData, place: PlaceSummary) -> None:
    """Add the Places guest rating, unless it just repeats one we have.

    Google Hotels and Google Places both surface the same underlying guest
    score, so printing both usually means saying one number twice under two
    names. When they genuinely disagree, both are kept - that disagreement is
    worth showing, and it is exactly why the source travels with the value.
    """
    if place.rating is None:
        return

    existing = option.user_rating
    if (
        existing is not None
        and existing.source == "google_hotels"
        and abs(existing.value - place.rating) < RATING_AGREEMENT
    ):
        # Same number, better review count. Keep the fuller one.
        if place.rating_count and (existing.review_count or 0) < place.rating_count:
            existing.review_count = place.rating_count
        return

    option.ratings.append(
        HotelRating(
            value=place.rating,
            scale_max=5.0,
            type="user_rating",
            source="google_places",
            review_count=place.rating_count,
        )
    )


def build_hotel_decision(
    ranked: list[RankedHotel], *, rationale: str | None = None
) -> Decision[HotelOptionData]:
    """A shortlist as the `hotel` Decision, with nothing selected.

    Choosing stays the traveller's; and a selection made here without a chosen
    area would fail integrity anyway, which is the ordering doing its job.
    """
    options = [
        DecisionOption[HotelOptionData](
            data=item.option,
            status="shortlisted" if position < 3 else "candidate",
            score=item.score,
            pros=item.pros,
            cons=item.cons,
        )
        for position, item in enumerate(ranked)
    ]

    return Decision[HotelOptionData](
        status="shortlisted" if options else "researching",
        options=options,
        rationale=rationale,
        updated_at=utcnow(),
    )


def _sort_price(option: HotelOptionData) -> float:
    if option.nightly_price:
        return option.nightly_price.amount
    cheapest = option.cheapest_quote
    # Unpriced last: it cannot be compared, and leading with it would suggest
    # it is a bargain.
    return cheapest.nightly.amount if cheapest and cheapest.nightly else float("inf")


def _expiry(options: list[HotelOptionData]):
    stated = [option.expires_at for option in options if option.expires_at]
    return min(stated) if stated else options[0].observed_at + QUOTE_TTL


def _apply_filters(
    options: list[HotelOptionData], spec: SearchHotelsInput, warnings: list[str]
) -> list[HotelOptionData]:
    """Filters the provider could not apply, each reporting what it removed.

    An empty result should be explained rather than mysterious - and a filter
    that silently removed everything is the most confusing outcome of all.
    """
    kept = list(options)

    if spec.max_nightly_price is not None:
        before = len(kept)
        kept = [
            option
            for option in kept
            if option.nightly_price is None or option.nightly_price.amount <= spec.max_nightly_price
        ]
        if before != len(kept):
            warnings.append(
                f"{before - len(kept)} hotel(s) were over {spec.max_nightly_price:.0f} per night"
            )

    if spec.min_rating is not None:
        before = len(kept)
        kept = [
            option
            for option in kept
            # No rating is not a low rating. Dropping unreviewed properties
            # would quietly delete anything newly opened.
            if option.user_rating is None or option.user_rating.value >= spec.min_rating
        ]
        if before != len(kept):
            warnings.append(
                f"{before - len(kept)} hotel(s) were rated below {spec.min_rating:g} by guests"
            )

    if spec.min_star_category is not None:
        before = len(kept)
        kept = [
            option
            for option in kept
            if option.star_category is None or option.star_category.value >= spec.min_star_category
        ]
        if before != len(kept):
            warnings.append(
                f"{before - len(kept)} hotel(s) were below {spec.min_star_category:g}-star category"
            )

    return kept
