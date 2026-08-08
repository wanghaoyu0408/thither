from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import new_id, utcnow

SourceType = Literal[
    "xiaohongshu",
    "reddit",
    "blog",
    "official",
    "publication",
    "other",
]

# Coarse on purpose. A numeric credibility score would be invented precision -
# there is no honest way to say one Reddit thread is 0.73 trustworthy.
Authority = Literal["official", "editorial", "community", "unknown"]

Sentiment = Literal["positive", "mixed", "negative", "unclear"]

# Quotes stay short and attributed; we summarize rather than reproduce.
MAX_QUOTE_WORDS = 15
MAX_SUMMARY_CHARS = 600


class EvidenceRecord(BaseModel):
    """Why a recommendation was made, kept for as long as the recommendation is.

    Deliberately separate from `PlaceEntity`, which holds only what Google
    asserts. Mixing the two would invite treating "someone on Reddit liked it"
    as the same kind of fact as "it opens at 11:30" (spec section 19).

    Distinct from the research cache too: that expires, this does not. Once a
    finding backs a shortlist, the justification has to survive its cache entry -
    including the `observed_at` of when it was actually retrieved.
    """

    evidence_id: str = Field(default_factory=lambda: new_id("ev"))

    # Always from a citation annotation, never from model prose. A model asked
    # for a source will invent a plausible URL.
    url: str
    title: str

    source_type: SourceType
    source_authority: Authority

    # Ours, and short. The page's text is never stored.
    summary: str = Field(max_length=MAX_SUMMARY_CHARS)
    quote: str | None = None

    # Places this supports, once Google has confirmed they exist.
    entity_ids: list[str] = []

    sentiment: Sentiment = "unclear"
    # Short qualitative notes: "queue is long", "good for groups".
    themes: list[str] = []

    published_at: datetime | None = None
    observed_at: datetime = Field(default_factory=utcnow)

    def trimmed_quote(self) -> str | None:
        """Cap the quote at a length that stays a citation, not a reproduction."""
        if not self.quote:
            return None
        words = self.quote.split()
        if len(words) <= MAX_QUOTE_WORDS:
            return self.quote
        return " ".join(words[:MAX_QUOTE_WORDS]) + "..."


# Where a source type sits on the coarse authority scale.
AUTHORITY_BY_SOURCE: dict[SourceType, Authority] = {
    "official": "official",
    "publication": "editorial",
    "blog": "community",
    "reddit": "community",
    "xiaohongshu": "community",
    "other": "unknown",
}


def authority_for(source_type: SourceType) -> Authority:
    return AUTHORITY_BY_SOURCE.get(source_type, "unknown")


class CommunitySignal(BaseModel):
    """What the community collectively said about one place.

    Derived from evidence at read time rather than stored, so it can never drift
    out of step with the records backing it. Counts are counts and sentiment is
    categorical - there is no synthesized score here either.
    """

    entity_id: str

    mention_count: int = 0
    source_count: int = 0
    source_types: list[SourceType] = []
    authorities: list[Authority] = []

    sentiment: Sentiment = "unclear"
    themes: list[str] = []
    evidence_ids: list[str] = []

    @property
    def has_editorial_backing(self) -> bool:
        return any(a in ("official", "editorial") for a in self.authorities)
