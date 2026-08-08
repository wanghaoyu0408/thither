"""Business-level web research (spec sections 19, 20 and 36).

Two things this layer is responsible for.

**Xiaohongshu must never be load-bearing.** Spec section 36 forbids scraping it,
forbids logged-in automation, and forbids depending on it. So the community tier
and the open-web tier are separate searches: an empty Xiaohongshu tier produces a
named warning and the pipeline carries on with Reddit and blogs. Only *every*
tier failing is an error.

**The cache and the evidence record are different things.** The cache exists to
avoid re-searching and expires after a fortnight. The moment a finding backs a
shortlist it is promoted to an `EvidenceRecord` in TripState, carrying the
`observed_at` of when it was actually retrieved. A cache entry lapsing must never
strip a stored recommendation of its justification.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.models.common import utcnow
from app.models.evidence import CommunitySignal, EvidenceRecord, Sentiment, authority_for
from app.models.research import ResearchResult, ResearchWebInput
from app.models.tool import ToolResult
from app.providers.openai_research import (
    PROVIDER,
    OpenAIResearchProvider,
    classify_source,
    trim_quote,
)
from app.services.cache import RESEARCH_POLICY, Cache, RequestDeduper, cache_key

# Community platforms, searched as their own tier so their absence is visible.
XIAOHONGSHU_DOMAINS = ["xiaohongshu.com", "xhslink.com"]
REDDIT_DOMAINS = ["reddit.com"]


@dataclass
class Tier:
    """One search pass over a named slice of the web."""

    name: str
    domains: list[str] | None
    # Xiaohongshu is explicitly allowed to come back empty (spec section 36).
    optional: bool = True


def default_tiers() -> list[Tier]:
    return [
        Tier("xiaohongshu", XIAOHONGSHU_DOMAINS),
        Tier("reddit", REDDIT_DOMAINS),
        Tier("open_web", None),
    ]


@dataclass
class ResearchRun:
    """A whole research pass, tier by tier, with what each one produced."""

    results: list[ResearchResult] = field(default_factory=list)
    empty_tiers: list[str] = field(default_factory=list)
    failed_tiers: dict[str, str] = field(default_factory=dict)
    # tier name -> what it did, in words. Whether Xiaohongshu came back empty,
    # failed, or was never asked is the whole question this milestone answers,
    # so it is recorded rather than left to be inferred from absence.
    tier_outcomes: dict[str, str] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def all_tiers_failed(self) -> bool:
        return bool(self.failed_tiers) and not self.results


class ResearchService:
    def __init__(
        self,
        provider: OpenAIResearchProvider,
        cache: Cache,
        deduper: RequestDeduper | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._deduper = deduper or RequestDeduper()

    async def research_web(
        self,
        spec: ResearchWebInput,
        *,
        tiers: list[Tier] | None = None,
    ) -> ToolResult[ResearchResult]:
        """Search each tier, merge, and report what came back empty."""
        run = await self.run_tiers(spec, tiers=tiers)

        warnings: list[str] = []
        for name in run.empty_tiers:
            warnings.append(f"{name} returned nothing for this query; other sources were used")
        for name, message in run.failed_tiers.items():
            warnings.append(f"{name} search failed: {message}")

        if run.all_tiers_failed:
            from app.models.tool import ToolError

            return ToolResult[ResearchResult](
                source=PROVIDER,
                warnings=warnings,
                error=ToolError(
                    code="provider_unavailable",
                    message="every research tier failed; no community signal is available",
                    provider=PROVIDER,
                    retryable=True,
                ),
            )

        return ToolResult[ResearchResult](source=PROVIDER, results=run.results, warnings=warnings)

    async def run_tiers(
        self, spec: ResearchWebInput, *, tiers: list[Tier] | None = None
    ) -> ResearchRun:
        run = ResearchRun()

        for tier in tiers if tiers is not None else default_tiers():
            # An explicit domain list on the input overrides the tier split.
            domains = spec.domains if spec.domains is not None else tier.domains
            tier_spec = spec.model_copy(update={"domains": domains})

            key = cache_key(
                "research",
                {
                    "q": spec.query,
                    "near": spec.near,
                    "purpose": spec.purpose,
                    "recency": spec.recency_days,
                    "domains": sorted(domains) if domains else None,
                },
            )

            cached = await self._cache.get(key, RESEARCH_POLICY)
            if cached is not None:
                run.results.extend(ResearchResult.model_validate(row) for row in cached)
                if cached:
                    run.tier_outcomes[tier.name] = f"{len(cached)} source(s), from cache"
                else:
                    run.empty_tiers.append(tier.name)
                    run.tier_outcomes[tier.name] = "nothing found (cached)"
                continue

            outcome = await self._deduper.run(
                key, lambda tier_spec=tier_spec: self._provider.research(tier_spec)
            )
            run.input_tokens += outcome.input_tokens
            run.output_tokens += outcome.output_tokens

            if not outcome.ok:
                run.failed_tiers[tier.name] = outcome.error.message
                run.tier_outcomes[tier.name] = f"failed: {outcome.error.message}"
                continue

            results = _to_results(outcome, tier.name)
            await self._cache.set(
                key, [result.model_dump(mode="json") for result in results], RESEARCH_POLICY
            )

            if not results:
                run.empty_tiers.append(tier.name)
                run.tier_outcomes[tier.name] = "nothing found"
                continue

            run.tier_outcomes[tier.name] = f"{len(results)} source(s)"
            run.results.extend(results)

        return run


def _to_results(outcome, tier_name: str) -> list[ResearchResult]:
    """One ResearchResult per cited source, with its mentions attached.

    Mentions with no citation are dropped: an unsourced claim is exactly what
    this milestone must not launder into the trip.
    """
    by_index = {citation.index: citation for citation in outcome.citations}
    grouped: dict[int, list] = {index: [] for index in by_index}

    for mention in outcome.mentions:
        if mention.citation_index in grouped:
            grouped[mention.citation_index].append(mention)

    results: list[ResearchResult] = []
    for index, citation in sorted(by_index.items()):
        results.append(
            ResearchResult(
                url=citation.url,
                title=citation.title or citation.url,
                source_type=classify_source(citation.url),
                summary=_summarize(outcome.text, grouped[index]),
                quote=None,
                mentioned_entities=grouped[index],
                tier=tier_name,
            )
        )
    return results


def _summarize(text: str, mentions: list) -> str:
    """A short line of our own. The page's text is never stored."""
    if mentions:
        named = ", ".join(dict.fromkeys(mention.name for mention in mentions))[:200]
        themes = ", ".join(
            dict.fromkeys(theme for mention in mentions for theme in mention.themes)
        )[:200]
        return f"Mentions {named}." + (f" Themes: {themes}." if themes else "")
    return (text or "").strip()[:280]


# --- evidence ----------------------------------------------------------------


def to_evidence(
    result: ResearchResult,
    *,
    entity_ids: list[str],
    observed_at: datetime | None = None,
) -> EvidenceRecord:
    """Promote a finding into the trip's permanent record of why.

    `observed_at` is the moment the research was retrieved, carried through
    deliberately: a cache entry lapsing must not make a stored recommendation
    look freshly justified, nor strip its justification away.
    """
    mentions = [m for m in result.mentioned_entities if m.entity_id in set(entity_ids)]
    themes = list(dict.fromkeys(theme for mention in mentions for theme in mention.themes))

    return EvidenceRecord(
        url=result.url,
        title=result.title[:300],
        source_type=result.source_type,
        source_authority=authority_for(result.source_type),
        summary=result.summary[:600],
        quote=trim_quote(result.quote),
        entity_ids=list(entity_ids),
        sentiment=_merge_sentiment([m.sentiment for m in mentions]),
        themes=themes[:8],
        published_at=result.published_at,
        observed_at=observed_at or utcnow(),
    )


def _merge_sentiment(values: list[Sentiment]) -> Sentiment:
    known = [value for value in values if value != "unclear"]
    if not known:
        return "unclear"
    unique = set(known)
    if len(unique) == 1:
        return known[0]
    return "mixed"


def signals_from_evidence(evidence: dict[str, EvidenceRecord]) -> dict[str, CommunitySignal]:
    """Roll evidence up per place, at read time.

    Derived rather than stored so it can never drift out of step with the
    records behind it. Counts are counts; nothing here is a synthesized score.
    """
    signals: dict[str, CommunitySignal] = {}

    for record in evidence.values():
        for entity_id in record.entity_ids:
            signal = signals.setdefault(entity_id, CommunitySignal(entity_id=entity_id))
            signal.mention_count += 1
            signal.evidence_ids.append(record.evidence_id)
            signal.source_types.append(record.source_type)
            signal.authorities.append(record.source_authority)
            signal.themes.extend(record.themes)

    for signal in signals.values():
        signal.source_count = len(set(signal.source_types))
        signal.source_types = list(dict.fromkeys(signal.source_types))
        signal.authorities = list(dict.fromkeys(signal.authorities))
        signal.themes = list(dict.fromkeys(signal.themes))[:8]
        signal.sentiment = _merge_sentiment(
            [
                evidence[evidence_id].sentiment
                for evidence_id in signal.evidence_ids
                if evidence_id in evidence
            ]
        )

    return signals
