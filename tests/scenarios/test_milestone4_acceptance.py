"""Milestone 4 acceptance, proved offline.

    A restaurant recommendation combines Google's facts with community signal,
    and keeps working when Xiaohongshu returns nothing.

The second half is the part that matters. Spec section 36 forbids depending on
Xiaohongshu at all, so the tests below deliberately starve the pipeline of it -
and then of every research source - and require recommendations anyway.
"""

from app.models.evidence import CommunitySignal
from app.models.place import PlaceSummary
from app.models.research import Citation, MentionedEntity
from app.models.tool import ToolError
from app.providers.openai_research import ResearchPass
from app.services.cache import InProcessCache, LayeredCache
from app.services.discovery_service import DiscoveryService
from app.services.place_service import PlaceService
from app.services.ranking_service import rank_places
from app.services.research_service import ResearchService

WIDE_HOURS = {
    "periods": [
        {"open": {"day": d, "hour": 9, "minute": 0}, "close": {"day": d, "hour": 23, "minute": 0}}
        for d in range(7)
    ]
}


def place(place_id: str, name: str, *, rating=4.4, count=800, **overrides) -> PlaceSummary:
    base = {
        "place_id": place_id,
        "name": name,
        "address": "Asakusa, Taito City, Tokyo",
        "lat": 35.714,
        "lng": 139.796,
        "categories": ["restaurant"],
        "rating": rating,
        "rating_count": count,
        "opening_hours": WIDE_HOURS,
    }
    return PlaceSummary(**{**base, **overrides})


GOOGLE_RESULTS = [
    place("ChIJ_futaku", "居酒屋 風鐸 FUTAKU", rating=4.9, count=980),
    place("ChIJ_totoya", "Sakaba Totoya", rating=4.7, count=3800),
    place("ChIJ_suitengu", "Izakaya Suitengu", rating=4.6, count=140),
]


class FakePlacesProvider:
    def __init__(self, results=None, details=None):
        self.results = results if results is not None else list(GOOGLE_RESULTS)
        self.details = details or {p.place_id: p for p in GOOGLE_RESULTS}
        self.searches: list[str] = []

    async def search_text(self, *, text_query, **kwargs):
        self.searches.append(text_query)
        return list(self.results)

    async def get_details(self, place_id, **kwargs):
        return self.details.get(place_id) or place(place_id, place_id)


class FakeResearchProvider:
    """Returns a scripted pass per tier, keyed by the domains it was asked for."""

    def __init__(self, by_domains: dict[str, ResearchPass]) -> None:
        self._by_domains = by_domains
        self.calls: list[list[str] | None] = []

    async def research(self, spec):
        self.calls.append(spec.domains)
        key = _tier_key(spec.domains)
        return self._by_domains.get(key, ResearchPass())


def _tier_key(domains) -> str:
    if not domains:
        return "open_web"
    if any("xiaohongshu" in d or "xhslink" in d for d in domains):
        return "xiaohongshu"
    if any("reddit" in d for d in domains):
        return "reddit"
    return "open_web"


def pass_with(
    url: str, title: str, name: str, *, themes=None, sentiment="positive"
) -> ResearchPass:
    return ResearchPass(
        text=f"People rate {name} highly. [1]",
        citations=[Citation(index=1, url=url, title=title)],
        mentions=[
            MentionedEntity(
                name=name,
                kind="restaurant",
                citation_index=1,
                sentiment=sentiment,
                themes=themes or ["worth the queue"],
            )
        ],
    )


REDDIT_PASS = pass_with(
    "https://www.reddit.com/r/JapanTravel/comments/abc/",
    "Best izakaya in Asakusa?",
    "Sakaba Totoya",
    themes=["locals go here", "cash only"],
)
BLOG_PASS = pass_with(
    "https://tokyocheapo.com/food/asakusa-izakaya/",
    "Asakusa izakaya guide",
    "居酒屋 風鐸 FUTAKU",
    themes=["tiny room", "book ahead"],
)
XHS_PASS = pass_with(
    "https://www.xiaohongshu.com/explore/xyz",
    "浅草居酒屋",
    "Izakaya Suitengu",
    themes=["photogenic"],
)


def build(research_by_tier: dict[str, ResearchPass] | None, places_provider=None):
    cache = LayeredCache(InProcessCache(), None)
    places = PlaceService(places_provider or FakePlacesProvider(), cache)
    research = (
        ResearchService(FakeResearchProvider(research_by_tier), cache)
        if research_by_tier is not None
        else None
    )
    return DiscoveryService(places, research), places, research


# --- the acceptance ----------------------------------------------------------


async def test_a_recommendation_carries_both_google_facts_and_community_signal():
    discovery, _, _ = build({"xiaohongshu": XHS_PASS, "reddit": REDDIT_PASS, "open_web": BLOG_PASS})

    outcome = await discovery.discover(query="izakaya", near="Asakusa, Tokyo")

    assert outcome.recommendations
    top = outcome.recommendations[0]

    # Google's half.
    assert top.ranked.place.rating is not None
    assert top.ranked.place.rating_count is not None
    # The community's half.
    assert top.evidence_ids
    assert all(eid in outcome.evidence for eid in top.evidence_ids)
    assert top.signal is not None and top.signal.mention_count >= 1
    assert outcome.google_only is False


async def test_xiaohongshu_returning_nothing_does_not_stop_the_recommendation():
    """Spec section 36: Xiaohongshu must never be load-bearing."""
    discovery, _, _ = build(
        {"xiaohongshu": ResearchPass(), "reddit": REDDIT_PASS, "open_web": BLOG_PASS}
    )

    outcome = await discovery.discover(query="izakaya", near="Asakusa, Tokyo")

    assert outcome.recommendations
    assert any("xiaohongshu returned nothing" in w for w in outcome.warnings)
    # And the signal that did arrive is still attached.
    assert any(rec.evidence_ids for rec in outcome.recommendations)


async def test_xiaohongshu_failing_outright_does_not_stop_the_recommendation():
    failing = ResearchPass(
        error=ToolError(code="provider_unavailable", message="blocked", provider="x")
    )
    discovery, _, _ = build({"xiaohongshu": failing, "reddit": REDDIT_PASS, "open_web": BLOG_PASS})

    outcome = await discovery.discover(query="izakaya", near="Asakusa, Tokyo")

    assert outcome.recommendations
    assert any("xiaohongshu search failed" in w for w in outcome.warnings)


async def test_every_research_source_failing_still_yields_google_recommendations():
    failing = ResearchPass(
        error=ToolError(code="provider_unavailable", message="down", provider="x")
    )
    discovery, _, _ = build({"xiaohongshu": failing, "reddit": failing, "open_web": failing})

    outcome = await discovery.discover(query="izakaya", near="Asakusa, Tokyo")

    assert outcome.recommendations, "Google alone must still produce recommendations"
    assert outcome.google_only is True
    assert any("no community signal available" in w for w in outcome.warnings)
    assert outcome.evidence == {}


async def test_no_research_configured_at_all_still_works():
    discovery, _, _ = build(None)

    outcome = await discovery.discover(query="izakaya", near="Asakusa, Tokyo")

    assert outcome.recommendations
    assert outcome.google_only is True
    assert any("research is not configured" in w for w in outcome.warnings)


async def test_a_dead_research_call_is_distinguishable_from_an_empty_one():
    """Spec section 38 again: silence and failure must not look alike."""
    empty, _, empty_research = build(
        {"xiaohongshu": ResearchPass(), "reddit": ResearchPass(), "open_web": ResearchPass()}
    )
    dead_pass = ResearchPass(
        error=ToolError(code="provider_unavailable", message="down", provider="x")
    )
    _, _, dead_research = build(
        {"xiaohongshu": dead_pass, "reddit": dead_pass, "open_web": dead_pass}
    )

    from app.models.research import ResearchWebInput

    quiet = await empty_research.research_web(ResearchWebInput(query="izakaya", near="Asakusa"))
    broken = await dead_research.research_web(ResearchWebInput(query="izakaya", near="Asakusa"))

    assert quiet.ok is True and quiet.found_nothing is True
    assert broken.ok is False and broken.error.code == "provider_unavailable"


# --- honesty about what could not be matched ---------------------------------


async def test_a_mention_google_cannot_match_is_reported_not_invented():
    ghost = pass_with(
        "https://www.reddit.com/r/JapanTravel/comments/zzz/",
        "Hidden gems",
        "A Place That Does Not Exist Anywhere",
    )
    discovery, _, _ = build({"reddit": ghost, "open_web": ResearchPass()})

    outcome = await discovery.discover(query="izakaya", near="Asakusa, Tokyo")

    assert outcome.unresolved_mentions
    unresolved = outcome.unresolved_mentions[0]
    assert unresolved.resolved is False
    assert unresolved.entity_id is None
    assert "unresolved" in (unresolved.resolution_note or "").lower()

    # And nothing inherited its signal.
    for record in outcome.evidence.values():
        assert "Does Not Exist" not in record.summary or record.entity_ids == []


async def test_evidence_only_ever_points_at_places_google_confirmed():
    discovery, _, _ = build({"reddit": REDDIT_PASS, "open_web": BLOG_PASS})

    outcome = await discovery.discover(query="izakaya", near="Asakusa, Tokyo")

    for record in outcome.evidence.values():
        for entity_id in record.entity_ids:
            assert entity_id in outcome.entities


# --- community signal cannot overrule the facts ------------------------------


def test_buzz_cannot_rescue_a_permanently_closed_place():
    beloved = CommunitySignal(
        entity_id="ent_shut", mention_count=9, source_count=5, sentiment="positive"
    )
    shut = place("ChIJ_shut", "Famous But Shut", business_status="CLOSED_PERMANENTLY")

    ranked = rank_places(
        [shut, place("ChIJ_open", "Ordinary But Open", rating=4.1, count=300)],
        signals={"ChIJ_shut": beloved},
    )

    assert [item.place.place_id for item in ranked] == ["ChIJ_open"]


def test_buzz_cannot_rescue_a_place_below_the_rating_floor():
    beloved = CommunitySignal(
        entity_id="ent_low", mention_count=9, source_count=5, sentiment="positive"
    )

    ranked = rank_places(
        [
            place("ChIJ_low", "Hyped But Poor", rating=3.4, count=900),
            place("ChIJ_good", "Quietly Good", rating=4.5, count=900),
        ],
        min_rating=4.0,
        signals={"ChIJ_low": beloved},
    )

    assert [item.place.place_id for item in ranked] == ["ChIJ_good"]


def test_buzz_does_reorder_places_that_both_pass():
    beloved = CommunitySignal(
        entity_id="ent_b", mention_count=6, source_count=4, sentiment="positive"
    )

    without = rank_places([place("ChIJ_a", "A", rating=4.5), place("ChIJ_b", "B", rating=4.4)])
    with_signal = rank_places(
        [place("ChIJ_a", "A", rating=4.5), place("ChIJ_b", "B", rating=4.4)],
        signals={"ChIJ_b": beloved},
    )

    assert [item.place.place_id for item in without] == ["ChIJ_a", "ChIJ_b"]
    assert with_signal[0].place.place_id == "ChIJ_b"
    assert "community" in with_signal[0].score.dimensions


def test_negative_sentiment_counts_against_rather_than_merely_not_for():
    disliked = CommunitySignal(
        entity_id="ent_x", mention_count=5, source_count=3, sentiment="negative"
    )

    neutral = rank_places([place("ChIJ_x", "X")])[0]
    panned = rank_places([place("ChIJ_x", "X")], signals={"ChIJ_x": disliked})[0]

    assert panned.score.total < neutral.score.total
    assert any("negative" in con for con in panned.cons)


def test_a_place_nobody_mentioned_is_not_penalised_for_it():
    """Absence of buzz is not evidence of badness; the dimension is skipped."""
    quiet = rank_places([place("ChIJ_q", "Q")])[0]

    assert "community" not in quiet.score.dimensions
    assert "community" in (quiet.score.notes or "")


# --- the cache is not the record ---------------------------------------------


async def test_cache_expiry_does_not_erase_evidence_behind_a_recommendation(sessions):
    """The research cache is disposable. The reason we recommended something is not."""
    from datetime import timedelta

    from app.models.research import ResearchResult
    from app.models.trip import TripState
    from app.services.cache import RESEARCH_POLICY, CachePolicy, ContentClass, SqliteCache
    from app.services.research_service import to_evidence

    durable = SqliteCache(sessions)
    finding = ResearchResult(
        url="https://www.reddit.com/r/JapanTravel/comments/abc/",
        title="Best izakaya",
        source_type="reddit",
        summary="Mentions Sakaba Totoya.",
    )

    # Cached, and about to lapse.
    await durable.set(
        "research:x",
        [finding.model_dump(mode="json")],
        CachePolicy(ContentClass.RESEARCH, ttl=timedelta(seconds=-1)),
    )

    # Promoted into the trip at the moment it backed a recommendation.
    state = TripState.new(title="Tokyo")
    record = to_evidence(finding, entity_ids=[])
    state.evidence[record.evidence_id] = record
    observed = record.observed_at

    assert await durable.purge_expired() == 1
    assert await durable.get("research:x", RESEARCH_POLICY) is None

    surviving = state.evidence[record.evidence_id]
    assert surviving.url == finding.url
    assert surviving.observed_at == observed
