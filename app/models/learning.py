"""What the system may learn about a traveller, and the shapes it learns in.

Two kinds of record live here, and the distinction carries the whole design:

  * A `LearningSignal` is a **stored fact** about something that happened -
    an activity moved later, a sentence said, a reflection submitted. It is
    written once, attributed to exactly one profile, and never interpreted
    at write time.
  * A `PreferenceHypothesis` is a **derived interpretation** - recomputed from
    the signals on every read, in the mould of `signals_from_evidence` and
    `conflict_service`, and never stored. Its identity is a content hash so
    that an answer ("not really") given today still matches the same
    hypothesis derived tomorrow (INVARIANTS section 5: a derived record needs
    a derived identity).

Strength and confidence are separate on purpose, the same discipline as
`DecisionScore.total` vs `coverage`: strength says how intensely the
preference was ever expressed, confidence says how much evidence there is.
Both are ordinal words, never floats - a percentage computed from three
clicks would be fake precision wearing a decimal point.
"""

import hashlib
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.common import new_id, utcnow

# The closed catalogue of things the system may learn. Closed because every
# key must name a real consumer - a learned preference that influences
# nothing is the recurring defect this codebase keeps finding (README ledger
# 10 and 11), and an open-ended key set would mint them freely.
PreferenceKey = Literal[
    "avoid_early_mornings",
    "relaxed_pace",
    "packed_pace",
    "parking_sensitive",
    "dislikes_queueing",
    # What choosing between priced options says. Each maps to an importance
    # weight that the rankers already multiply by - see the catalogue's
    # `consumer` field for which line of which ranker.
    "values_nonstop",
    "flight_price_sensitive",
    "hotel_location_matters",
    "hotel_price_sensitive",
]

# Ordinal, never a float. "weak" is a click, "moderate" is a post-trip
# statement, "strong" is the traveller saying it in words.
SignalStrength = Literal["weak", "moderate", "strong"]

SignalSource = Literal[
    "behavior_move",
    "behavior_replan",
    "behavior_choice",
    "stated",
    "reflection",
]

# v1 emits only "stronger" - every catalogue key is phrased so that evidence
# pushes toward it. The axis exists so contrary evidence can be recorded
# without a schema change when a consumer for it exists.
Direction = Literal["stronger", "weaker"]

Confidence = Literal["emerging", "likely", "strong"]

HypothesisStatus = Literal["emerging", "proposable", "applied", "dismissed"]


class LearningSignal(BaseModel):
    """One observed fact about one traveller. Append-only, never a conclusion.

    `context` carries whatever makes the eventual "Why?" readable without the
    trip that produced it - trip titles, item names, clock times, quotes.
    Learning must outlive its trips, so the signal is self-describing.
    """

    signal_id: str = Field(default_factory=lambda: new_id("sig"))

    profile_id: str
    trip_id: str

    preference_key: PreferenceKey
    direction: Direction = "stronger"
    strength: SignalStrength
    source: SignalSource

    context: dict[str, Any] = {}

    observed_at: datetime = Field(default_factory=utcnow)


class HypothesisEvidence(BaseModel):
    """One signal, rendered as the line "Why?" will show.

    Rendered server-side so the UI has no formatting logic and the acceptance
    that "Why? uses persisted evidence only" is provable where the evidence
    lives.
    """

    signal_id: str
    trip_id: str
    source: SignalSource
    strength: SignalStrength
    line: str
    observed_at: datetime


class PreferenceHypothesis(BaseModel):
    """A pattern the evidence currently supports. Derived on every read.

    Never stored: storing an interpretation lets it drift from the facts
    behind it. The id is a content hash of what the hypothesis *is* - so a
    dismissal recorded against it still matches the next derivation, however
    much evidence has arrived since.
    """

    hypothesis_id: str = ""

    profile_id: str
    preference_key: PreferenceKey
    direction: Direction = "stronger"

    # How intensely this was ever expressed vs how much evidence there is.
    # Never folded into one another; never numbers.
    strength: SignalStrength
    confidence: Confidence
    status: HypothesisStatus

    # Where an acceptance would land, and what it would write.
    field_path: str
    proposed_value: Any = None

    summary: str

    signal_ids: list[str] = []
    trip_ids: list[str] = []
    evidence: list[HypothesisEvidence] = []

    @model_validator(mode="after")
    def _identify(self) -> "PreferenceHypothesis":
        if not self.hypothesis_id:
            seed = "|".join([self.profile_id, self.preference_key, self.direction])
            digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
            object.__setattr__(self, "hypothesis_id", f"hyp_{digest}")
        return self


class LearnedProvenance(BaseModel):
    """Why a profile field holds the value it holds.

    Stored on the profile at acceptance, keyed by dotted field path. This is
    what makes "why is this in my profile?" answerable from stored state
    alone, and `previous_value` is what Remove reverts to - a hand-set 09:30
    that learning raised to 10:30 must go back to 09:30, not to the model
    default.
    """

    hypothesis_id: str
    preference_key: PreferenceKey

    signal_ids: list[str] = []
    trip_ids: list[str] = []

    previous_value: Any = None
    # "proposed" when the card's value was accepted as-is; "edited" when the
    # traveller adjusted it on the way in.
    value_source: Literal["proposed", "edited"] = "proposed"

    summary: str
    accepted_at: datetime = Field(default_factory=utcnow)


class ReflectionItem(BaseModel):
    """An itinerary item named in a reflection, denormalized while it exists."""

    item_id: str
    label: str


class TripReflection(BaseModel):
    """The traveller's own account of how the trip went. Answered once.

    Written through the patch engine so it is audited and cannot be re-asked.
    `loved` and `notes` are stored, not parsed - free text becomes a learning
    signal only when the agent records it explicitly with the traveller named.
    `answered_by` is the attribution: on a group trip the signals from this
    reflection belong to whoever answered, never to a guess.
    """

    days_too_busy: list[date] = []
    loved: list[str] = []
    skipped: list[ReflectionItem] = []
    notes: str | None = None

    answered_by: str
    submitted_at: datetime = Field(default_factory=utcnow)
