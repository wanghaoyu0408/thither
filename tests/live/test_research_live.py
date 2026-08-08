"""Real web research, opt-in.

    .\\.venv\\Scripts\\python.exe -m pytest -m live --override-ini addopts=

Asserts on *shape* - citations exist, they are on the domains we asked for,
mentions carry no URLs - never on which restaurants come back, which change with
the index and the model's mood.
"""

import pytest

from app.config import get_settings
from app.models.research import ResearchWebInput
from app.providers.openai_research import classify_source
from app.services.cache import InProcessCache, LayeredCache
from app.services.research_service import REDDIT_DOMAINS, ResearchService, Tier
from app.services.toolbox import Toolbox

settings = get_settings()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (settings.openai_api_key and settings.google_maps_api_key),
        reason="needs OPENAI_API_KEY and GOOGLE_MAPS_API_KEY",
    ),
]


@pytest.fixture
async def toolbox():
    async with Toolbox(settings) as box:
        yield box


async def test_research_returns_real_cited_sources(toolbox):
    result = await toolbox.research.research_web(
        ResearchWebInput(
            query="best ramen locals recommend",
            near="Shinjuku, Tokyo",
            purpose="restaurant_discovery",
        ),
        tiers=[Tier("reddit", REDDIT_DOMAINS)],
    )

    assert result.ok, result.error
    if result.found_nothing:
        pytest.skip("the index had nothing for this query today; that is a valid outcome")

    for row in result.results:
        assert row.url.startswith("http")
        # The domain filter was honoured, so classification agrees with the tier.
        assert classify_source(row.url) == "reddit", row.url
        assert row.summary
        # Mentions reference sources by index; a URL here would mean the model
        # wrote one, which is exactly what the design forbids.
        for mention in row.mentioned_entities:
            assert "http" not in mention.name


async def test_the_pipeline_combines_google_facts_with_community_signal(toolbox):
    """The acceptance criterion, against real APIs."""
    outcome = await toolbox.discovery.discover(
        query="izakaya", near="Asakusa, Tokyo", limit=4, min_rating=4.0
    )

    assert outcome.recommendations, outcome.warnings

    for rec in outcome.recommendations:
        # Google's half is never optional.
        assert rec.ranked.place.name
        assert rec.ranked.place.rating is not None

    if outcome.google_only:
        # A legitimate outcome, and it has to be stated rather than hidden.
        assert any("community signal" in w or "returned nothing" in w for w in outcome.warnings)
    else:
        backed = [rec for rec in outcome.recommendations if rec.evidence_ids]
        assert backed, "research ran but nothing was attached to a recommendation"
        for rec in backed:
            for evidence_id in rec.evidence_ids:
                record = outcome.evidence[evidence_id]
                assert record.url.startswith("http")
                assert record.entity_ids == [rec.entity_id]


async def test_a_xiaohongshu_only_search_never_breaks_the_caller(toolbox):
    """Whatever the index holds today, this must not raise or error out."""
    from app.services.research_service import XIAOHONGSHU_DOMAINS

    cache = LayeredCache(InProcessCache(), None)
    research = ResearchService(toolbox.research._provider, cache)

    result = await research.research_web(
        ResearchWebInput(query="ramen", near="Tokyo", purpose="restaurant_discovery"),
        tiers=[Tier("xiaohongshu", XIAOHONGSHU_DOMAINS)],
    )

    # Either it found something, or it says it found nothing. Both are fine;
    # raising would not be.
    assert result.ok or result.error.code in ("provider_unavailable", "rate_limited")
