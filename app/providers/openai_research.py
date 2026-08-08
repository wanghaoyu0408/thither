"""Web research via OpenAI's hosted web_search tool (spec sections 19 and 35).

Two passes, for one reason: **a model asked for a source will write a plausible
URL that does not exist.**

    1. web_search -> prose plus `url_citation` annotations. The annotations are
       the only place a real URL comes from. They are numbered and the prose is
       marked up `[1]`, `[2]` at their offsets.
    2. A structured pass over that marked-up prose, extracting mentioned places
       that point at a citation *by index*.

So the model never emits a URL, only an integer. An index that does not resolve
is dropped rather than guessed - the same discipline as everywhere else here.

The second pass also sidesteps whether web_search and a json_schema response
format can be combined in a single call: they need not be.
"""

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from openai import APIError, AsyncOpenAI

from app.models.evidence import MAX_QUOTE_WORDS, SourceType
from app.models.research import Citation, MentionedEntity, ResearchWebInput
from app.models.tool import ToolError

PROVIDER = "openai_research"

# Host fragments that identify where a page came from. Checked longest-first so
# a more specific host wins.
_SOURCE_HOSTS: tuple[tuple[str, SourceType], ...] = (
    ("xiaohongshu.com", "xiaohongshu"),
    ("xhslink.com", "xiaohongshu"),
    ("reddit.com", "reddit"),
    ("redd.it", "reddit"),
    ("tabelog.com", "publication"),
    ("timeout.com", "publication"),
    ("cntraveler.com", "publication"),
    ("japantimes.co.jp", "publication"),
    ("michelin.com", "publication"),
    ("tripadvisor.com", "publication"),
    ("go.jp", "official"),
    ("gotokyo.org", "official"),
    ("japan.travel", "official"),
)


def classify_source(url: str) -> SourceType:
    """Where a page came from, by host. Unknown hosts are treated as blogs."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "other"
    if not host:
        return "other"

    for fragment, source_type in _SOURCE_HOSTS:
        if host == fragment or host.endswith(f".{fragment}"):
            return source_type
    return "blog"


def trim_quote(text: str | None) -> str | None:
    """Keep quotes to a citation's length rather than a reproduction."""
    if not text:
        return None
    words = text.split()
    return text if len(words) <= MAX_QUOTE_WORDS else " ".join(words[:MAX_QUOTE_WORDS]) + "..."


@dataclass
class ResearchPass:
    """What one web_search call produced."""

    text: str = ""
    citations: list[Citation] = field(default_factory=list)
    mentions: list[MentionedEntity] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: ToolError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "restaurant",
                            "cafe",
                            "bar",
                            "attraction",
                            "shop",
                            "area",
                            "other",
                        ],
                    },
                    "citation_index": {
                        "type": ["integer", "null"],
                        "description": "The [n] marker that mentions this place. Never a URL.",
                    },
                    "sentiment": {
                        "type": "string",
                        "enum": ["positive", "mixed", "negative", "unclear"],
                    },
                    "themes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Short qualitative notes: 'long queue', 'good for groups'.",
                    },
                    "note": {"type": ["string", "null"]},
                },
                "required": ["name", "kind", "citation_index", "sentiment", "themes", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["mentions"],
    "additionalProperties": False,
}

SEARCH_INSTRUCTIONS = """\
You are researching places for a traveller. Search the web and report what people
actually say: which specific venues get recommended, and why.

Name venues precisely, as they are written on the page. Say what the sentiment is
and what the recurring themes are - queues, atmosphere, value, whether it suits a
group.

Do not state opening hours, addresses, prices or travel times as fact; those are
verified elsewhere. If the sources disagree, say so. If you find nothing useful,
say that plainly rather than padding.
"""

EXTRACTION_INSTRUCTIONS = """\
Extract every specific venue named in the text below.

The text carries markers like [1] and [2] identifying its sources. For each venue,
set citation_index to the marker nearest the mention. If no marker applies, use
null. Never output a URL - only the integer.

Use the venue's name as written. Do not invent places that are not in the text.
"""


class OpenAIResearchProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def research(self, spec: ResearchWebInput) -> ResearchPass:
        """Search, then extract. Returns prose, real citations, and mentions."""
        tool: dict[str, Any] = {"type": "web_search"}
        if spec.domains:
            # Up to 100 domains, no scheme, subdomains included.
            tool["filters"] = {"allowed_domains": spec.domains[:100]}

        prompt = spec.query
        if spec.near:
            prompt = f"{prompt} (in or near {spec.near})"
        if spec.recency_days:
            prompt = f"{prompt}. Prefer sources from the last {spec.recency_days} days."

        try:
            response = await self._client.responses.create(
                model=self._model,
                instructions=SEARCH_INSTRUCTIONS,
                input=[{"role": "user", "content": prompt}],
                tools=[tool],
            )
        except APIError as exc:
            return ResearchPass(
                error=ToolError(
                    code="provider_unavailable",
                    message=f"{type(exc).__name__}: {exc}",
                    provider=PROVIDER,
                    retryable=True,
                )
            )

        text, citations = _collect(response)
        usage = getattr(response, "usage", None)
        result = ResearchPass(
            text=text,
            citations=citations,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

        if not text.strip():
            return result

        result.mentions = await self._extract(text, citations, result)
        return result

    async def _extract(
        self, text: str, citations: list[Citation], carrier: ResearchPass
    ) -> list[MentionedEntity]:
        valid = {citation.index for citation in citations}

        try:
            response = await self._client.responses.create(
                model=self._model,
                instructions=EXTRACTION_INSTRUCTIONS,
                input=[{"role": "user", "content": text}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "mentions",
                        "schema": EXTRACTION_SCHEMA,
                        "strict": True,
                    }
                },
            )
        except APIError:
            # Losing the extraction is survivable; the prose and citations still
            # stand, and the caller reports that nothing was extracted.
            return []

        usage = getattr(response, "usage", None)
        carrier.input_tokens += getattr(usage, "input_tokens", 0) or 0
        carrier.output_tokens += getattr(usage, "output_tokens", 0) or 0

        payload = _first_json(response)
        mentions: list[MentionedEntity] = []
        for raw in payload.get("mentions", []):
            index = raw.get("citation_index")
            if index is not None and index not in valid:
                # A marker that does not exist. Keep the mention, drop the claim
                # that a particular source backs it.
                index = None
            mentions.append(
                MentionedEntity(
                    name=(raw.get("name") or "").strip(),
                    kind=raw.get("kind") or "other",
                    citation_index=index,
                    sentiment=raw.get("sentiment") or "unclear",
                    themes=[t for t in (raw.get("themes") or []) if t][:6],
                    note=raw.get("note"),
                )
            )
        return [mention for mention in mentions if mention.name]


def _collect(response: Any) -> tuple[str, list[Citation]]:
    """Pull the prose out, and mark it up with numbered citation markers.

    Markers are inserted from the end backwards so earlier offsets stay valid.
    """
    chunks: list[str] = []
    citations: list[Citation] = []
    by_url: dict[str, int] = {}

    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue

        for part in getattr(item, "content", []) or []:
            text = getattr(part, "text", None)
            if not text:
                continue

            insertions: list[tuple[int, str]] = []
            for annotation in getattr(part, "annotations", []) or []:
                if getattr(annotation, "type", None) != "url_citation":
                    continue
                url = getattr(annotation, "url", None)
                if not url:
                    continue

                if url not in by_url:
                    by_url[url] = len(by_url) + 1
                    citations.append(
                        Citation(
                            index=by_url[url],
                            url=url,
                            title=getattr(annotation, "title", "") or "",
                        )
                    )
                end = getattr(annotation, "end_index", None)
                if end is not None:
                    insertions.append((int(end), f"[{by_url[url]}]"))

            for offset, marker in sorted(insertions, reverse=True):
                offset = max(0, min(offset, len(text)))
                text = text[:offset] + marker + text[offset:]

            chunks.append(text)

    return "\n".join(chunks), citations


def _first_json(response: Any) -> dict[str, Any]:
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            text = getattr(part, "text", None)
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}
