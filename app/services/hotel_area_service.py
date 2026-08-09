"""Choosing a neighbourhood before choosing a hotel (spec section 25).

The spec is unusually prescriptive here, and says why: *"Store `hotel_area` as a
separate Decision. This is important."* Picking a hotel first and rationalising
the location afterwards is how people end up commuting forty minutes to
everything they came for.

    anchors        what the trip has actually scheduled or shortlisted
    candidates     clusters of those anchors, plus any neighbourhood suggested
    routes         real travel minutes from each candidate to each anchor
    research       what people say about staying there, when M4 can find any
    decision       Decision[HotelAreaOption], ranked and explainable

An area is scored on **real travel time** to the places this trip will visit.
Not straight-line distance, which spec section 21 forbids substituting, and not
the provider's own `location_rating`, which is a composite nobody can unpack.
Kilometres appear only as a sanity figure alongside the minutes that matter.

Quietness is not scored. Spec section 23 lists it as a hotel dimension and no
provider supplies it; where research mentions noise that becomes a stated
theme, attributed. An invented quietness number would be exactly the false
precision the evidence layer exists to prevent.
"""

from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from app.models.common import LatLng, utcnow
from app.models.decision import (
    Decision,
    DecisionOption,
    DecisionScore,
    HotelAreaOption,
)
from app.models.entity import PlaceEntity
from app.models.evidence import Sentiment
from app.models.hotel import HotelAreaCandidate
from app.models.place import PlaceFieldSet, SearchPlacesInput
from app.models.research import ResearchResult, ResearchWebInput
from app.models.route import GetRoutesInput, LocationRef, RouteLeg, TravelMode
from app.models.tool import ToolError, ToolResult
from app.models.trip import TripState
from app.services.clustering import cluster_places
from app.services.geo import centroid, haversine_km
from app.services.place_service import PlaceService
from app.services.research_service import ResearchService
from app.services.route_service import RouteService
from app.services.scoring import combine, ranking_value

SOURCE = "hotel_area"

# Areas x anchors is a matrix, and transit caps at 100 elements per call. Route
# service chunks past that, but each chunk is billed, so both sides are capped
# at something a person could actually weigh up.
MAX_ANCHORS = 12
MAX_AREAS = 6

# Beyond this, an area stops being "a bit further" and becomes a daily commute.
CEILING_MINUTES = 45.0

# How wide a net to cast when geocoding a neighbourhood name around the trip.
GEOCODE_RADIUS_METERS = 40_000

# Mid-morning: transit timetables differ by hour, and this is when a day out
# actually starts.
DEPARTURE_HOUR = 10

# What to measure with when the asked-for mode has no coverage.
#
# Google answers transit queries in the US and the UK and returns no route at
# all in Japan, which is precisely where this project's example trip goes. That
# is a licensing gap in Google's data, not a failure: the call succeeds and
# every pair comes back unreachable.
#
# Falling back keeps the comparison possible. What must never happen is a
# driving minute being read as a train minute, so the mode actually used is
# carried on every result and named in the warnings.
FALLBACK_MODE: dict[TravelMode, TravelMode] = {
    "transit": "driving",
    "bicycling": "walking",
}


async def measure_travel(
    routes: RouteService,
    *,
    origins: list[LocationRef],
    destinations: list[LocationRef],
    entities: dict[str, PlaceEntity],
    mode: TravelMode,
    departure_at: datetime | None = None,
) -> tuple[TravelMode, ToolResult[RouteLeg], list[str]]:
    """A route matrix, retried in another mode if the first has no coverage.

    Only retries when *every* pair came back unreachable. A few missing pairs
    is ordinary - some places genuinely have no route between them - and
    switching modes over that would trade a true answer for a comparable one.

    Deliberately not in `route_service`: for the itinerary validator "no transit
    route" has to stay unknown rather than quietly becoming a driving time. Here
    the substitution is a considered choice about ranking, and it is declared.
    """
    warnings: list[str] = []

    async def run(travel_mode: TravelMode) -> ToolResult[RouteLeg]:
        return await routes.get_routes(
            GetRoutesInput(
                origins=origins,
                destinations=destinations,
                mode=travel_mode,
                departure_at=departure_at,
            ),
            entities=entities,
        )

    result = await run(mode)
    if not result.ok:
        return mode, result, warnings

    if any(leg.status == "ok" for leg in result.results):
        return mode, result, warnings

    fallback = FALLBACK_MODE.get(mode)
    if fallback is None:
        return mode, result, warnings

    retried = await run(fallback)
    if not retried.ok or not any(leg.status == "ok" for leg in retried.results):
        return mode, result, warnings

    warnings.append(
        f"no {mode} route was found for any pair - Google has no {mode} data for this "
        f"region - so these are {fallback} times instead. They are real measured times, "
        f"but they are not {mode} times."
    )
    return fallback, retried, warnings


_SENTIMENT_SCORE: dict[Sentiment, float | None] = {
    "positive": 1.0,
    "mixed": 0.5,
    "negative": 0.0,
    # No opinion is not a neutral opinion, so it scores nothing at all.
    "unclear": None,
}


class AreaSignal(BaseModel):
    """What research said about staying in one neighbourhood.

    Not an `EvidenceRecord`: that registry keys on entity ids and an area is not
    an entity, so promoting this into `TripState.evidence` would either break
    referential integrity or require inventing an entity for a neighbourhood.
    The sources are carried here instead, with their URLs, so the claim stays
    attributable wherever it is rendered.
    """

    sentiment: Sentiment = "unclear"
    themes: list[str] = []
    source_urls: list[str] = []
    source_titles: list[str] = []

    @property
    def source_count(self) -> int:
        return len(self.source_urls)


class RankedArea(BaseModel):
    candidate: HotelAreaCandidate
    score: DecisionScore

    mode: TravelMode = "transit"

    # The figures behind the score, kept so the recommendation can be quoted
    # back with real numbers rather than adjectives.
    mean_minutes: float | None = None
    worst_minutes: float | None = None
    minutes_by_anchor: dict[str, float] = {}
    unreachable_anchors: list[str] = []
    anchor_count: int = 0

    signal: AreaSignal | None = None

    pros: list[str] = []
    cons: list[str] = []

    @property
    def reachable(self) -> int:
        return len(self.minutes_by_anchor)


# --- anchors -----------------------------------------------------------------


def committed_anchor_ids(state: TripState) -> list[str]:
    """Places this trip has actually committed to, strongest first.

    Scheduled items lead: something on the itinerary is a firmer statement of
    intent than something merely shortlisted, and when the cap bites it is the
    shortlist that should give way.
    """
    ordered: list[str] = []

    for _day, item in state.itinerary.iter_items():
        if item.entity_id and item.entity_id not in ordered:
            ordered.append(item.entity_id)

    for _name, decision in state.decisions.iter_decisions():
        for option in decision.options:
            entity_id = getattr(option.data, "entity_id", None)
            if entity_id and entity_id not in ordered:
                ordered.append(entity_id)

    return [entity_id for entity_id in ordered if entity_id in state.entities]


def anchor_entities(state: TripState, *, limit: int = MAX_ANCHORS) -> list[PlaceEntity]:
    """The places an area's convenience is measured against.

    Falls back to the whole registry when nothing is scheduled or shortlisted
    yet. Discovery fills the registry before an itinerary exists, and refusing
    to rank areas until a trip has days would put this step *after* the
    planning it is supposed to inform. The fallback is weaker evidence - a place
    found is not a place chosen - so `recommend_areas` says when it used it.
    """
    ordered = committed_anchor_ids(state) or sorted(state.entities)
    return [state.entities[entity_id] for entity_id in ordered[:limit]]


def departure_time_for(state: TripState, *, now: datetime | None = None) -> datetime:
    """A mid-morning departure at the destination, for transit timetables.

    Google rejects a departure time in the past, so a trip whose dates have
    passed - or which has none yet - is routed for tomorrow instead. The shape
    of a city's transit network on a normal morning is what is being measured;
    the exact date matters much less than it being a plausible one.
    """
    reference = now or utcnow()

    zone: tzinfo = UTC
    if state.brief.timezone:
        try:
            zone = ZoneInfo(state.brief.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = UTC

    day = state.brief.dates.start or (reference.astimezone(zone).date() + timedelta(days=1))
    departure = datetime.combine(day, time(hour=DEPARTURE_HOUR), tzinfo=zone)

    if departure <= reference:
        tomorrow = reference.astimezone(zone).date() + timedelta(days=1)
        departure = datetime.combine(tomorrow, time(hour=DEPARTURE_HOUR), tzinfo=zone)

    return departure.astimezone(UTC)


# --- candidates --------------------------------------------------------------


def cluster_candidates(anchors: list[PlaceEntity], count: int) -> list[HotelAreaCandidate]:
    """Neighbourhoods derived from where the trip already goes.

    These need no model and no network call: if half the trip is in one part of
    the city, the middle of that half is a candidate whether or not anyone has
    heard of it by name.
    """
    if not anchors:
        return []

    points = {entity.entity_id: (entity.lat, entity.lng) for entity in anchors}
    by_id = {entity.entity_id: entity for entity in anchors}

    candidates: list[HotelAreaCandidate] = []
    for cluster in cluster_places(points, min(count, len(points))):
        if not cluster.entity_ids:
            continue
        lat, lng = cluster.center
        # Named after the anchor nearest its middle. "near Senso-ji" is a
        # location a person can picture; "cluster 2" is not.
        nearest = min(
            cluster.entity_ids,
            key=lambda eid: (haversine_km(lat, lng, *points[eid]), eid),
        )
        candidates.append(
            HotelAreaCandidate(
                area_name=f"near {by_id[nearest].name}",
                lat=lat,
                lng=lng,
                origin="trip_cluster",
                anchor_entity_ids=sorted(cluster.entity_ids),
            )
        )
    return candidates


def _dedupe_candidates(candidates: list[HotelAreaCandidate]) -> list[HotelAreaCandidate]:
    """Drop candidates that are effectively the same place.

    A named neighbourhood and a cluster centre a few hundred metres apart would
    otherwise appear twice and split the comparison. The named one wins: it is
    what a person would recognise.
    """
    kept: list[HotelAreaCandidate] = []
    for candidate in sorted(candidates, key=lambda c: c.origin != "suggested"):
        if any(
            haversine_km(candidate.lat, candidate.lng, other.lat, other.lng) < 0.8 for other in kept
        ):
            continue
        kept.append(candidate)
    return kept


# --- scoring -----------------------------------------------------------------


def _travel_score(minutes: float | None) -> float | None:
    if minutes is None:
        return None
    return round(max(0.0, 1.0 - minutes / CEILING_MINUTES), 4)


def score_area(
    *,
    mean_minutes: float | None,
    worst_minutes: float | None,
    reachable: int,
    anchor_count: int,
    signal: AreaSignal | None,
) -> DecisionScore:
    """Dimensions, weights, and an honest note about what was missing."""
    coverage = reachable / anchor_count if anchor_count else None

    community = _SENTIMENT_SCORE.get(signal.sentiment) if signal else None

    components: dict[str, tuple[float | None, float]] = {
        # How far the average outing is.
        "mean_travel": (_travel_score(mean_minutes), 1.0),
        # And the worst one, because a single 70-minute anchor is what makes an
        # area annoying to stay in, and a mean hides it.
        "worst_travel": (_travel_score(worst_minutes), 0.6),
        # Somewhere with no route to half the trip is not "slightly worse".
        "coverage": (coverage, 0.8),
        "community": (community, 0.4),
    }

    return combine(components)


def _pros_and_cons(
    minutes_by_anchor: dict[str, float],
    unreachable: list[str],
    names: dict[str, str],
    signal: AreaSignal | None,
) -> tuple[list[str], list[str]]:
    pros: list[str] = []
    cons: list[str] = []

    if minutes_by_anchor:
        closest = min(minutes_by_anchor, key=lambda eid: (minutes_by_anchor[eid], eid))
        pros.append(f"{minutes_by_anchor[closest]:.0f} min to {names.get(closest, closest)}")

        within = sum(1 for value in minutes_by_anchor.values() if value <= 20.0)
        if within:
            pros.append(f"{within} of {len(minutes_by_anchor)} anchor(s) within 20 minutes")

        furthest = max(minutes_by_anchor, key=lambda eid: (minutes_by_anchor[eid], eid))
        if minutes_by_anchor[furthest] > CEILING_MINUTES:
            cons.append(f"{minutes_by_anchor[furthest]:.0f} min to {names.get(furthest, furthest)}")

    if unreachable:
        listed = ", ".join(names.get(eid, eid) for eid in unreachable[:3])
        cons.append(f"no route found to {listed}")

    if signal and signal.themes:
        pros.extend(f"mentioned as: {theme}" for theme in signal.themes[:3])
    if signal and signal.sentiment == "negative":
        cons.append(f"{signal.source_count} source(s) were negative about staying here")

    return pros, cons


# --- the service -------------------------------------------------------------


class HotelAreaService:
    def __init__(
        self,
        places: PlaceService,
        routes: RouteService,
        research: ResearchService | None = None,
    ) -> None:
        self._places = places
        self._routes = routes
        self._research = research

    async def recommend_areas(
        self,
        state: TripState,
        *,
        suggested_areas: list[str] | None = None,
        mode: TravelMode = "transit",
        limit: int = 5,
        with_research: bool = True,
        now: datetime | None = None,
    ) -> ToolResult[RankedArea]:
        warnings: list[str] = []

        anchors = anchor_entities(state)
        if not anchors:
            # A precondition, not an empty result. Ranking neighbourhoods by
            # convenience to nothing would produce a confident answer about
            # nothing, so it refuses and names the step that comes first.
            return ToolResult[RankedArea](
                source=SOURCE,
                error=ToolError(
                    code="invalid_request",
                    message=(
                        "this trip knows of no places yet, so there is nothing to measure an "
                        "area's convenience against. Search or discover places first."
                    ),
                    provider=SOURCE,
                ),
            )

        if not committed_anchor_ids(state):
            warnings.append(
                f"nothing is scheduled or shortlisted yet, so these areas are ranked against "
                f"all {len(anchors)} place(s) found so far rather than against choices"
            )

        names = {entity.entity_id: entity.name for entity in anchors}
        middle = centroid([(entity.lat, entity.lng) for entity in anchors])

        candidates = cluster_candidates(anchors, count=3)

        if suggested_areas:
            geocoded, problems = await self._geocode(suggested_areas, state, middle)
            candidates.extend(geocoded)
            warnings.extend(problems)

        candidates = _dedupe_candidates(candidates)[:MAX_AREAS]
        if not candidates:
            return ToolResult[RankedArea](
                source=SOURCE,
                warnings=[*warnings, "no candidate neighbourhood could be located"],
            )

        signals: dict[str, AreaSignal] = {}
        if with_research and self._research is not None:
            signals, research_warnings = await self._area_research(candidates, state)
            warnings.extend(research_warnings)

        origins = [
            LocationRef(lat=candidate.lat, lng=candidate.lng, label=candidate.area_name)
            for candidate in candidates
        ]
        destinations = [
            LocationRef(entity_id=entity.entity_id, label=entity.name) for entity in anchors
        ]

        mode, routes, fallback_warnings = await measure_travel(
            self._routes,
            origins=origins,
            destinations=destinations,
            entities=state.entities,
            mode=mode,
            departure_at=departure_time_for(state, now=now),
        )
        warnings.extend(fallback_warnings)
        warnings.extend(routes.warnings)

        if not routes.ok:
            # No travel times means no comparison. Saying "these areas look
            # similar" here would be a guess wearing a ranking's clothes.
            error = routes.error.model_copy(
                update={
                    "message": (
                        f"areas cannot be compared without travel times: {routes.error.message}"
                    )
                }
            )
            return ToolResult[RankedArea](source=SOURCE, warnings=warnings, error=error)

        minutes: dict[int, dict[str, float]] = {index: {} for index in range(len(candidates))}
        for leg in routes.results:
            if leg.status != "ok" or leg.duration_minutes is None:
                continue
            anchor = anchors[leg.destination_index]
            minutes[leg.origin_index][anchor.entity_id] = round(leg.duration_minutes, 1)

        ranked: list[RankedArea] = []
        for index, candidate in enumerate(candidates):
            reached = minutes[index]
            unreachable = [
                entity.entity_id for entity in anchors if entity.entity_id not in reached
            ]
            values = list(reached.values())

            mean_minutes = round(sum(values) / len(values), 1) if values else None
            worst_minutes = max(values) if values else None
            signal = signals.get(candidate.area_name)

            pros, cons = _pros_and_cons(reached, unreachable, names, signal)

            ranked.append(
                RankedArea(
                    candidate=candidate,
                    score=score_area(
                        mean_minutes=mean_minutes,
                        worst_minutes=worst_minutes,
                        reachable=len(reached),
                        anchor_count=len(anchors),
                        signal=signal,
                    ),
                    mode=mode,
                    mean_minutes=mean_minutes,
                    worst_minutes=worst_minutes,
                    minutes_by_anchor=reached,
                    unreachable_anchors=unreachable,
                    anchor_count=len(anchors),
                    signal=signal,
                    pros=pros,
                    cons=cons,
                )
            )

        if not any(area.minutes_by_anchor for area in ranked):
            # Every pair unreachable in every mode tried. There is no basis for
            # a comparison, and ranking on community sentiment alone would be a
            # confident answer to a question nothing was measured for.
            return ToolResult[RankedArea](
                source=SOURCE,
                warnings=warnings,
                error=ToolError(
                    code="provider_unavailable",
                    message=(
                        "no travel time could be measured between any candidate area and any "
                        "of this trip's places, so the areas cannot be compared. Nothing here "
                        "is a ranking."
                    ),
                    provider=SOURCE,
                    retryable=False,
                ),
            )

        # Mean minutes breaks a tie before the name does, so two areas that score
        # alike are separated by the figure the traveller actually feels.
        ranked.sort(
            key=lambda area: (
                -ranking_value(area.score),
                area.mean_minutes if area.mean_minutes is not None else 1e9,
                area.candidate.area_name,
            )
        )

        return ToolResult[RankedArea](
            source=SOURCE,
            results=ranked[:limit],
            warnings=warnings,
            expires_at=routes.expires_at,
        )

    async def _geocode(
        self,
        names: list[str],
        state: TripState,
        middle: tuple[float, float] | None,
    ) -> tuple[list[HotelAreaCandidate], list[str]]:
        """Turn neighbourhood names into coordinates, or say which failed."""
        if middle is None:
            return [], ["no anchor coordinates to search around; suggested areas were skipped"]

        city = state.brief.destination.city
        found: list[HotelAreaCandidate] = []
        problems: list[str] = []

        for name in dict.fromkeys(names):
            # The city qualifier matters: "Mission" without "San Francisco"
            # lands wherever Google feels like.
            query = f"{name}, {city}" if city else name
            result = await self._places.search_places(
                SearchPlacesInput(
                    query=query,
                    lat=middle[0],
                    lng=middle[1],
                    radius_meters=GEOCODE_RADIUS_METERS,
                    limit=1,
                    field_set=PlaceFieldSet.BASIC,
                )
            )
            if not result.ok:
                problems.append(f"could not locate {name!r}: {result.error.message}")
                continue

            place = next(
                (p for p in result.results if p.lat is not None and p.lng is not None), None
            )
            if place is None:
                problems.append(f"{name!r} did not match anywhere near this trip")
                continue

            found.append(
                HotelAreaCandidate(
                    area_name=name,
                    lat=place.lat,
                    lng=place.lng,
                    origin="suggested",
                )
            )

        return found, problems

    async def _area_research(
        self, candidates: list[HotelAreaCandidate], state: TripState
    ) -> tuple[dict[str, AreaSignal], list[str]]:
        """One search for the whole question, not one per candidate.

        "Where should we stay in Tokyo" is the query people actually write, and
        it returns comparisons; six separate "is Shinjuku good" searches cost
        six times as much and return six pieces of unrelated advocacy.
        """
        assert self._research is not None
        city = state.brief.destination.city
        if not city:
            return {}, ["no destination city set; neighbourhood research was skipped"]

        result = await self._research.research_web(
            ResearchWebInput(
                query=f"best neighbourhoods to stay in {city} and what each is like",
                purpose="neighborhood_research",
                near=city,
            )
        )
        if not result.ok:
            # Research is a bonus dimension. Losing it costs colour, not
            # correctness, so the ranking goes ahead on travel times alone.
            return {}, [f"neighbourhood research unavailable: {result.error.message}"]

        signals = _match_signals(candidates, result.results)
        warnings = list(result.warnings)
        if not signals:
            warnings.append(
                "research found no comment on these specific areas; they are ranked on "
                "travel time alone"
            )
        return signals, warnings


def _match_signals(
    candidates: list[HotelAreaCandidate], results: list[ResearchResult]
) -> dict[str, AreaSignal]:
    """Attach what was said about an area to that area, by name.

    Only `kind == "area"` mentions are considered. A restaurant named after the
    district it sits in would otherwise turn a dinner recommendation into an
    opinion about where to sleep.
    """
    signals: dict[str, AreaSignal] = {}

    for candidate in candidates:
        needle = candidate.area_name.removeprefix("near ").strip().lower()
        if not needle:
            continue

        sentiments: list[Sentiment] = []
        themes: list[str] = []
        urls: list[str] = []
        titles: list[str] = []

        for result in results:
            hits = [
                mention
                for mention in result.mentioned_entities
                if mention.kind == "area" and needle in mention.name.strip().lower()
            ]
            if not hits:
                continue
            sentiments.extend(mention.sentiment for mention in hits)
            themes.extend(theme for mention in hits for theme in mention.themes)
            urls.append(result.url)
            titles.append(result.title)

        if not urls:
            continue

        known = [value for value in sentiments if value != "unclear"]
        if not known:
            sentiment: Sentiment = "unclear"
        elif len(set(known)) == 1:
            sentiment = known[0]
        else:
            sentiment = "mixed"

        signals[candidate.area_name] = AreaSignal(
            sentiment=sentiment,
            themes=list(dict.fromkeys(themes))[:6],
            source_urls=list(dict.fromkeys(urls)),
            source_titles=list(dict.fromkeys(titles)),
        )

    return signals


# --- the decision ------------------------------------------------------------


def build_area_decision(
    ranked: list[RankedArea],
    *,
    select_best: bool = False,
    rationale: str | None = None,
) -> Decision[HotelAreaOption]:
    """Ranked areas as the `hotel_area` Decision spec section 25 asks for.

    `select_best` defaults to False: ranking is the agent's job, choosing is the
    traveller's. A shortlist presented for confirmation is the honest output of
    a comparison the user has not seen yet.
    """
    options: list[DecisionOption[HotelAreaOption]] = []

    for position, area in enumerate(ranked):
        options.append(
            DecisionOption[HotelAreaOption](
                data=HotelAreaOption(
                    area_name=area.candidate.area_name,
                    center=LatLng(lat=area.candidate.lat, lng=area.candidate.lng),
                    anchor_entity_ids=area.candidate.anchor_entity_ids,
                    notes=_area_note(area),
                ),
                status="shortlisted" if position < 3 else "candidate",
                score=area.score,
                pros=area.pros,
                cons=area.cons,
                # Left empty by design: evidence records key on entity ids and a
                # neighbourhood is not an entity, so promoting area research
                # into the registry would mean inventing one. The sources live
                # in the note above, with their URLs. See AreaSignal.
                evidence_refs=[],
            )
        )

    decision = Decision[HotelAreaOption](
        status="shortlisted" if options else "researching",
        options=options,
        rationale=rationale,
        updated_at=utcnow(),
    )

    if select_best and options:
        decision.selected_option_id = options[0].option_id
        decision.status = "selected"

    return decision


def _area_note(area: RankedArea) -> str:
    """The figures behind the ranking, in one line, all of them from a tool."""
    parts: list[str] = []
    if area.mean_minutes is not None:
        parts.append(
            f"{area.mean_minutes:.0f} min mean {area.mode} time to "
            f"{area.reachable}/{area.anchor_count} anchor(s)"
        )
    if area.worst_minutes is not None:
        parts.append(f"worst {area.worst_minutes:.0f} min")
    if area.signal and area.signal.source_urls:
        cited = ", ".join(area.signal.source_urls[:2])
        parts.append(f"{area.signal.sentiment} in {area.signal.source_count} source(s): {cited}")
    return "; ".join(parts)
