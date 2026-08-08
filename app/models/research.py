from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.evidence import MAX_SUMMARY_CHARS, Sentiment, SourceType

ResearchPurpose = Literal[
    "restaurant_discovery",
    "activity_discovery",
    "hotel_research",
    "neighborhood_research",
    "destination_research",
    "general",
]


class ResearchWebInput(BaseModel):
    """Discovery and qualitative research (spec section 19).

    Never a source of opening hours, addresses, travel times or prices - those
    are verified against structured providers before they reach a user.
    """

    query: str = Field(min_length=1)

    # Host names without a scheme, e.g. ["reddit.com"]. Subdomains included.
    domains: list[str] | None = None

    purpose: ResearchPurpose = "general"
    recency_days: int | None = None

    # Where the research is about, used to resolve mentions afterwards.
    near: str | None = None

    max_results: int = Field(default=8, ge=1, le=20)


class Citation(BaseModel):
    """A real source, taken from the model's citation annotations.

    The index is what the extraction pass refers to. Nothing downstream ever
    accepts a URL written in prose.
    """

    index: int
    url: str
    title: str = ""


class MentionedEntity(BaseModel):
    """A place named in a source, before Google has confirmed it exists."""

    name: str
    kind: Literal["restaurant", "cafe", "bar", "attraction", "shop", "area", "other"] = "other"

    # Which citation backs this. Out-of-range indices are dropped, not guessed.
    citation_index: int | None = None

    sentiment: Sentiment = "unclear"
    themes: list[str] = []
    note: str | None = None

    # Filled in by resolution_service once matched to a real place.
    entity_id: str | None = None
    resolved: bool = False
    resolution_note: str | None = None


class ResearchResult(BaseModel):
    """One source, normalized (spec section 19)."""

    url: str
    title: str

    source_type: SourceType
    published_at: datetime | None = None

    summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    quote: str | None = None

    mentioned_entities: list[MentionedEntity] = []

    # Which search tier produced this, so an empty tier can be reported by name.
    tier: str = "open_web"
