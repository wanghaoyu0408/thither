"""Why something was recommended, assembled from what was stored at the time.

The product rule this exists to enforce (spec sections 16 and 41): *a
recommendation is not complete unless the system can explain why it was made and
what trade-off it represents* - and the explanation must come from persisted
decision data, not from asking a model to justify a choice after the fact. A
model asked "why did you pick this?" will always produce something fluent, and
it will sometimes be fiction.

So nothing here generates prose. It gathers the score, the pros and cons, the
group split and the evidence that were recorded when the choice was made, and
hands them to the caller as structure. When the trip does not hold enough to
answer, it says so - `complete=False` - rather than filling the gap.

Facts and opinions stay apart on the way out, too. Google saying a place opens
at 22:00 and Reddit saying it is atmospheric are different kinds of claim, and
`EvidenceRecord.source_authority` already distinguishes them, so the grouping is
structural rather than a matter of how the renderer feels.
"""

from pydantic import BaseModel

from app.models.decision import DecisionOption, DecisionScore
from app.models.evidence import Authority, EvidenceRecord
from app.models.group import GroupScore
from app.models.trip import TripState

# How `source_authority` maps onto what a reader should do with a claim.
_EVIDENCE_GROUPS: dict[Authority, str] = {
    "official": "official",
    "editorial": "editorial",
    "community": "community",
    "unknown": "community",
}


class EvidenceView(BaseModel):
    """One source, with enough to judge it and a link to check it."""

    evidence_id: str
    title: str
    url: str
    source_type: str
    authority: Authority
    summary: str
    quote: str | None = None
    sentiment: str = "unclear"
    themes: list[str] = []
    observed_at: str | None = None


class TravelerView(BaseModel):
    name: str
    score: float


class Explanation(BaseModel):
    """Everything the trip actually knows about why this was chosen."""

    entity_id: str
    name: str

    # False when nothing was stored. The caller must say so rather than
    # improvise; that is the whole point of the field.
    complete: bool = False
    # Named when incomplete, so the gap is specific rather than a shrug.
    missing: list[str] = []

    # Where the recommendation lives, e.g. "place_shortlists.izakaya_asakusa".
    decision: str | None = None
    purpose: str | None = None
    status: str | None = None

    score: DecisionScore | None = None
    pros: list[str] = []
    cons: list[str] = []

    # Per traveller, never collapsed - the M7 rule applies here too.
    group_total: float | None = None
    group_spread: float | None = None
    group_is_split: bool = False
    worst_served: str | None = None
    per_traveler: list[TravelerView] = []

    # Grouped by what kind of claim each source is making.
    official: list[EvidenceView] = []
    editorial: list[EvidenceView] = []
    community: list[EvidenceView] = []

    # Facts Google asserted about the place itself.
    rating: float | None = None
    rating_count: int | None = None
    price_level: int | None = None
    serves_vegetarian: str = "unknown"

    @property
    def evidence_count(self) -> int:
        return len(self.official) + len(self.editorial) + len(self.community)

    @property
    def has_group_split(self) -> bool:
        return self.group_is_split


def _evidence_view(record: EvidenceRecord) -> EvidenceView:
    return EvidenceView(
        evidence_id=record.evidence_id,
        title=record.title,
        url=record.url,
        source_type=record.source_type,
        authority=record.source_authority,
        summary=record.summary,
        # Trimmed at the model, so a citation can never become a reproduction.
        quote=record.trimmed_quote(),
        sentiment=record.sentiment,
        themes=record.themes,
        observed_at=record.observed_at.isoformat() if record.observed_at else None,
    )


def find_option(
    state: TripState, entity_id: str
) -> tuple[str, DecisionOption] | tuple[None, None]:
    """The stored decision option that recommended this place, if any.

    Walks every decision including the keyed shortlists, so a place picked for
    dinner on day 1 is found the same way as one picked for lunch on day 3.
    """
    for name, decision in state.decisions.iter_decisions():
        for option in decision.options:
            if getattr(option.data, "entity_id", None) == entity_id:
                return name, option
    return None, None


def explain(state: TripState, entity_id: str) -> Explanation:
    """Assemble the explanation. Reads only; invents nothing."""
    entity = state.entities.get(entity_id)
    explanation = Explanation(
        entity_id=entity_id,
        name=entity.name if entity else entity_id,
    )

    if entity is not None:
        explanation.rating = entity.rating
        explanation.rating_count = entity.rating_count
        explanation.price_level = entity.price_level
        explanation.serves_vegetarian = entity.serves_vegetarian
    else:
        explanation.missing.append("this place is not in the trip's registry")

    name, option = find_option(state, entity_id)
    if option is None:
        # The honest outcome, and a common one for anything scheduled before
        # discovery started recording its reasoning.
        explanation.missing.append(
            "no stored decision recommended this place, so the reasoning behind it "
            "was not recorded"
        )
        return explanation

    explanation.decision = name
    explanation.status = option.status
    explanation.purpose = getattr(option.data, "purpose", None)
    explanation.score = option.score
    explanation.pros = list(option.pros)
    explanation.cons = list(option.cons)

    group = option.group_score
    if isinstance(group, GroupScore) and group.per_traveler:
        explanation.group_total = group.total
        explanation.group_spread = group.spread
        explanation.group_is_split = group.is_split
        explanation.worst_served = group.name_of(group.worst_traveler_id)
        explanation.per_traveler = [
            TravelerView(name=group.name_of(traveler_id), score=value)
            for traveler_id, value in group.ranked_travelers()
        ]

    for evidence_id in option.evidence_refs:
        record = state.evidence.get(evidence_id)
        if record is None:
            # A dangling ref would already fail integrity, so this is defensive.
            explanation.missing.append(f"evidence {evidence_id} is no longer stored")
            continue
        view = _evidence_view(record)
        getattr(explanation, _EVIDENCE_GROUPS[record.source_authority]).append(view)

    if option.score is None:
        explanation.missing.append("no ranking was recorded for this option")
    if not option.pros and not option.cons:
        explanation.missing.append("no trade-off was recorded for this option")

    # Complete means the two things a recommendation owes the reader: a reason
    # it was chosen, and what it costs. Evidence strengthens it but a place can
    # be defensibly chosen on Google data alone (spec section 36).
    explanation.complete = bool(option.score and (option.pros or option.cons))
    return explanation


def explain_item(state: TripState, item_id: str) -> Explanation | None:
    """The explanation behind a scheduled item, via the place it points at."""
    for _day, item in state.itinerary.iter_items():
        if item.item_id != item_id:
            continue
        if not item.entity_id:
            # Free time and generic transport have no place to explain.
            return Explanation(
                entity_id="",
                name=item.title,
                missing=["this item is not a place, so there is nothing to explain"],
            )
        return explain(state, item.entity_id)
    return None
