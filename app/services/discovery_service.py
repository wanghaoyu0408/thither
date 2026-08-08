"""The restaurant discovery pipeline (spec section 20).

    preferences
       -> Google Places candidates        facts: it exists, it opens, it is here
        + web research                    taste: whether it is any good
       -> entity resolution               a name in prose -> a place Google confirms
       -> Google Place details            hours, for the shortlist only
       -> ranking with community signal
       -> 3-5 recommendations, each carrying its evidence

Run by code rather than orchestrated by the model, for the reason M3 taught: a
pipeline the model is asked to remember is a pipeline that sometimes does not
happen.

The load-bearing property is that **every stage after Google is optional**. If
research fails entirely, or Xiaohongshu is simply not indexed for this query,
the pipeline still returns recommendations - with less to say about them, and
saying so.
"""

from dataclasses import dataclass, field

from app.models.evidence import CommunitySignal, EvidenceRecord
from app.models.place import GetPlaceDetailsInput, PlaceFieldSet, PlaceSummary, SearchPlacesInput
from app.models.research import MentionedEntity, ResearchResult, ResearchWebInput
from app.services.entity_service import resolve_places
from app.services.place_service import PlaceService
from app.services.ranking_service import RankedPlace, rank_places
from app.services.research_service import ResearchService, Tier, signals_from_evidence, to_evidence
from app.services.resolution_service import apply_match, match_mention

MAX_MENTIONS_TO_RESOLVE = 12


@dataclass
class Recommendation:
    ranked: RankedPlace
    entity_id: str
    evidence_ids: list[str] = field(default_factory=list)
    signal: CommunitySignal | None = None


@dataclass
class DiscoveryOutcome:
    recommendations: list[Recommendation] = field(default_factory=list)
    entities: dict = field(default_factory=dict)
    evidence: dict[str, EvidenceRecord] = field(default_factory=dict)

    unresolved_mentions: list[MentionedEntity] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # What each research tier actually did. Absence of Xiaohongshu evidence is
    # ambiguous on its own - it could mean empty, failed, or found-but-unmatched
    # - so the outcome is recorded rather than inferred.
    research_tiers: dict[str, str] = field(default_factory=dict)

    # True when Google returned candidates but no community signal was found.
    google_only: bool = False

    @property
    def found_nothing(self) -> bool:
        return not self.recommendations


class DiscoveryService:
    def __init__(self, places: PlaceService, research: ResearchService | None) -> None:
        self._places = places
        self._research = research

    async def discover(
        self,
        *,
        query: str,
        near: str,
        existing_entities: dict | None = None,
        limit: int = 5,
        min_rating: float | None = 4.0,
        min_rating_count: int | None = 50,
        tiers: list[Tier] | None = None,
        with_details: bool = True,
    ) -> DiscoveryOutcome:
        outcome = DiscoveryOutcome()
        registry = dict(existing_entities or {})

        # --- 1. Google candidates. Without these there is nothing to recommend.
        found = await self._places.search_places(
            SearchPlacesInput(query=f"{query} in {near}", lat=0.0, lng=0.0, limit=20)
        )
        if not found.ok:
            outcome.warnings.append(f"place search failed: {found.error.message}")
            return outcome
        if found.found_nothing:
            outcome.warnings.append(f"Google found no {query} in {near}")
            return outcome

        candidates: dict[str, PlaceSummary] = {place.place_id: place for place in found.results}

        # --- 2. Community signal. Everything from here is best-effort.
        research_results: list[ResearchResult] = []
        if self._research is None:
            outcome.warnings.append("research is not configured; using Google data only")
        else:
            # run_tiers rather than research_web, so the per-tier outcome is
            # available to report instead of only the merged results.
            run = await self._research.run_tiers(
                ResearchWebInput(query=f"best {query}", near=near, purpose="restaurant_discovery"),
                tiers=tiers,
            )
            outcome.research_tiers = dict(run.tier_outcomes)
            for name in run.empty_tiers:
                outcome.warnings.append(
                    f"{name} returned nothing for this query; other sources were used"
                )
            for name, message in run.failed_tiers.items():
                outcome.warnings.append(f"{name} search failed: {message}")

            if run.all_tiers_failed:
                # Spec section 38: a dead search is not an empty neighbourhood.
                outcome.warnings.append(
                    "no community signal available; recommendations rest on Google data alone"
                )
            else:
                research_results = run.results

        # --- 3. Resolve mentions against places Google confirms.
        resolved_by_place: dict[str, list[tuple[ResearchResult, MentionedEntity]]] = {}
        seen_names: set[str] = set()

        for result in research_results:
            updated: list[MentionedEntity] = []
            for mention in result.mentioned_entities:
                if len(seen_names) >= MAX_MENTIONS_TO_RESOLVE and mention.name not in seen_names:
                    continue
                seen_names.add(mention.name)

                place, match = await self._resolve(mention, near, candidates)
                mention = apply_match(mention, match, place.place_id if place else None)
                updated.append(mention)

                if mention.resolved and place is not None:
                    candidates.setdefault(place.place_id, place)
                    resolved_by_place.setdefault(place.place_id, []).append((result, mention))
                else:
                    outcome.unresolved_mentions.append(mention)
            result.mentioned_entities = updated

        # --- 4. Details for the shortlist only (spec section 18).
        shortlist_ids = _shortlist_ids(candidates, resolved_by_place, limit)
        if with_details and shortlist_ids:
            details = await self._places.get_place_details(
                GetPlaceDetailsInput(place_ids=shortlist_ids, field_set=PlaceFieldSet.FULL)
            )
            if details.ok:
                for place in details.results:
                    candidates[place.place_id] = place
            else:
                outcome.warnings.append(f"details lookup failed: {details.error.message}")

        # --- 5. Registry entities, so everything downstream refers to ids.
        entities = resolve_places([candidates[pid] for pid in shortlist_ids], registry)
        by_place_id = {entity.provider_refs["google_place_id"]: entity for entity in entities}
        outcome.entities = {entity.entity_id: entity for entity in entities}

        # --- 6. Evidence, keyed by the place it actually backs.
        for place_id, pairs in resolved_by_place.items():
            entity = by_place_id.get(place_id)
            if entity is None:
                continue
            for result, mention in pairs:
                record = to_evidence(result, entity_ids=[entity.entity_id])
                record.sentiment = mention.sentiment
                record.themes = list(dict.fromkeys(mention.themes))[:8]
                outcome.evidence[record.evidence_id] = record

        signals_by_entity = signals_from_evidence(outcome.evidence)
        signals_by_place = {
            place_id: signals_by_entity[entity.entity_id]
            for place_id, entity in by_place_id.items()
            if entity.entity_id in signals_by_entity
        }

        # --- 7. Rank. Hard filters run before signal is consulted.
        ranked = rank_places(
            [candidates[pid] for pid in shortlist_ids],
            min_rating=min_rating,
            min_rating_count=min_rating_count,
            limit=limit,
            signals=signals_by_place,
        )

        for item in ranked:
            entity = by_place_id.get(item.place.place_id)
            if entity is None:
                continue
            signal = signals_by_entity.get(entity.entity_id)
            outcome.recommendations.append(
                Recommendation(
                    ranked=item,
                    entity_id=entity.entity_id,
                    evidence_ids=list(signal.evidence_ids) if signal else [],
                    signal=signal,
                )
            )

        outcome.google_only = not outcome.evidence
        if outcome.google_only and not outcome.found_nothing:
            outcome.warnings.append(
                "no community signal matched these places; ranking used Google data only"
            )
        return outcome

    async def _resolve(
        self, mention: MentionedEntity, near: str, candidates: dict[str, PlaceSummary]
    ):
        """Try the places already in hand, then ask Google specifically."""
        match = match_mention(mention.name, list(candidates.values()))
        if match.accepted:
            return match.place, match

        found = await self._places.search_places(
            SearchPlacesInput(query=f"{mention.name} {near}", lat=0.0, lng=0.0, limit=5)
        )
        if not found.ok or found.found_nothing:
            from app.services.resolution_service import Match

            return None, Match(
                None,
                "none",
                f"Google returned nothing for {mention.name!r}; left unresolved",
            )

        match = match_mention(mention.name, found.results)
        return (match.place if match.accepted else None), match


def _shortlist_ids(
    candidates: dict[str, PlaceSummary],
    resolved: dict[str, list],
    limit: int,
) -> list[str]:
    """Places worth spending a details call on.

    Anything the community named comes first - that is the whole point of having
    researched - then the best-rated Google candidates fill the rest.
    """
    ordered = list(resolved)
    remaining = [
        place_id
        for place_id in sorted(
            candidates,
            key=lambda pid: (-(candidates[pid].rating or 0.0), pid),
        )
        if place_id not in resolved
    ]
    return (ordered + remaining)[: max(limit * 2, limit)]
