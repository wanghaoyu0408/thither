"""Turning a name someone wrote on the internet into a place Google confirms.

A mention is a string in prose. An entity is somewhere that demonstrably exists,
at coordinates, with hours. The gap between them is where a travel agent can do
real damage: guess wrong and the trip contains a confident recommendation for a
place nobody meant.

So matching is deterministic and conservative, and **an unresolved mention stays
unresolved**. It is reported, it contributes no signal, and it never becomes a
place. Half the value of this module is the matches it refuses to make.
"""

import re
import unicodedata
from dataclasses import dataclass

from app.models.place import PlaceSummary
from app.models.research import MentionedEntity

# Words that carry no identifying weight when comparing venue names.
_NOISE = frozenset(
    {
        "the",
        "a",
        "an",
        "restaurant",
        "cafe",
        "café",
        "coffee",
        "bar",
        "shop",
        "store",
        "tokyo",
        "japan",
        "branch",
        "main",
        "honten",
        "ten",
    }
)

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Fold a venue name to something comparable across scripts and styles.

    NFKC first, so full-width Japanese characters and their half-width forms
    compare equal - the same venue is routinely written both ways.
    """
    folded = unicodedata.normalize("NFKC", name).casefold()
    folded = _PUNCTUATION.sub(" ", folded)
    return _WHITESPACE.sub(" ", folded).strip()


def tokens(name: str) -> set[str]:
    return {token for token in normalize(name).split() if token and token not in _NOISE}


def _squash(name: str) -> str:
    """Normalized, with word breaks removed.

    Japanese place names are written with and without separators more or less
    interchangeably in English text.
    """
    return normalize(name).replace(" ", "")


@dataclass
class Match:
    place: PlaceSummary | None
    confidence: str  # "exact" | "strong" | "weak" | "none"
    note: str

    @property
    def accepted(self) -> bool:
        return self.place is not None and self.confidence in ("exact", "strong")


def match_mention(mention: str, candidates: list[PlaceSummary]) -> Match:
    """Best Google candidate for a mentioned name, or an honest refusal."""
    if not candidates:
        return Match(None, "none", "no Google candidates were returned for this name")

    wanted = normalize(mention)
    wanted_tokens = tokens(mention)
    wanted_squashed = _squash(mention)

    if not wanted_tokens:
        return Match(None, "none", f"{mention!r} carries no distinguishing words")

    scored: list[tuple[int, float, PlaceSummary]] = []
    for place in candidates:
        if not place.name:
            continue
        candidate = normalize(place.name)
        candidate_tokens = tokens(place.name)

        if candidate == wanted or _squash(place.name) == wanted_squashed:
            # The squashed comparison catches word-break differences in the same
            # name - "Shimo-Kitazawa" against "Shimokitazawa" - which are the
            # same place written two ways, not two places. Strictly narrower
            # than token overlap, so it cannot create a false match on its own.
            scored.append((0, 1.0, place))
            continue

        if not candidate_tokens:
            continue

        overlap = len(wanted_tokens & candidate_tokens)
        if not overlap:
            continue

        # Containment either way: "Fuglen" mentioned, "Fuglen Tokyo" returned,
        # and the reverse, are both the same place.
        coverage = overlap / min(len(wanted_tokens), len(candidate_tokens))
        if wanted_tokens <= candidate_tokens or candidate_tokens <= wanted_tokens:
            scored.append((1, coverage, place))
        elif coverage >= 0.6:
            scored.append((2, coverage, place))

    if not scored:
        return Match(
            None,
            "none",
            f"no Google result's name overlaps {mention!r}; left unresolved rather than guessed",
        )

    scored.sort(key=lambda row: (row[0], -row[1], row[2].place_id))
    rank, coverage, place = scored[0]

    if rank == 0:
        return Match(place, "exact", f"exact name match on {place.name!r}")
    if rank == 1:
        return Match(place, "strong", f"{place.name!r} contains the mentioned name")
    return Match(place, "weak", f"{place.name!r} only partly overlaps {mention!r}")


def apply_match(mention: MentionedEntity, match: Match, entity_id: str | None) -> MentionedEntity:
    """Record the outcome on the mention, resolved or not."""
    if match.accepted and entity_id:
        return mention.model_copy(
            update={
                "entity_id": entity_id,
                "resolved": True,
                "resolution_note": match.note,
            }
        )
    return mention.model_copy(
        update={"entity_id": None, "resolved": False, "resolution_note": match.note}
    )


def unresolved(mentions: list[MentionedEntity]) -> list[MentionedEntity]:
    return [mention for mention in mentions if not mention.resolved]
