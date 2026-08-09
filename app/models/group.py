"""Group preferences, group scores and the conflicts between them (spec sections 23-24).

The acceptance for this milestone is phrased as a prohibition: identify major
preference conflicts *instead of hiding them behind an average score*. The thing
prohibited is the obvious implementation, so the models here are shaped to make
it awkward.

Two travelers scoring 0.9 and 0.1 average to 0.5. Two scoring 0.5 and 0.5 also
average to 0.5. One of those is a fight and the other is consensus, and no
single number can tell them apart. `GroupScore` therefore keeps every
traveler's score, names who is worst served, and renders itself with the split
attached - the same device `HotelRating.describe()` uses to stop a star category
being read as a guest rating.
"""

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.common import utcnow
from app.models.traveler import (
    ActivityPreferences,
    FlightPreferences,
    FoodPreferences,
    HotelPreferences,
    PacePreferences,
)

# A gap this wide between the happiest and the unhappiest traveler is a split
# rather than a rounding difference. Below it, an option really is roughly
# equally liked and the mean is a fair summary.
SPLIT_THRESHOLD = 0.35

# How much the worst-served traveler counts against the group average. A pure
# mean lets three people outvote one into a trip they will hate; pure maximin
# lets one lukewarm person veto everything. This sits nearer the mean while
# still making one ruined traveler expensive.
WORST_WEIGHT = 0.4


class TravelerPreferences(BaseModel):
    """One traveler's preferences, resolved and frozen into the trip.

    A snapshot, not a lookup. Profiles live in their own table and can change
    long after a trip is planned; if planning read them live, editing a profile
    next year would silently rewrite why last year's trip was built that way.
    Same rule M4 applied to evidence: once a finding is load-bearing, it is
    stored with its provenance rather than re-fetched.
    """

    flight: FlightPreferences = FlightPreferences()
    hotel: HotelPreferences = HotelPreferences()
    food: FoodPreferences = FoodPreferences()
    activity: ActivityPreferences = ActivityPreferences()
    pace: PacePreferences = PacePreferences()

    # Where this came from, so "why is price weighted 0.9 for Bob?" is
    # answerable from stored state alone.
    source_profile_id: str | None = None
    source_profile_revision: int | None = None

    # Dotted paths the trip overrode, e.g. "flight.price_importance". Recorded
    # because a trip-specific override outranks the profile, and which of the
    # two spoke is exactly what someone will ask about later.
    overridden_paths: list[str] = []

    resolved_at: datetime = Field(default_factory=utcnow)

    def section(self, name: str):
        """One preference block by name, for code that walks categories."""
        return getattr(self, name, None)


class GroupScore(BaseModel):
    """How a whole group feels about one option.

    `per_traveler` is the point of the model and is never collapsed away. The
    summary statistics are derived from it and are meaningless without it.
    """

    per_traveler: dict[str, float] = {}
    # traveler_id -> display name, so nothing has to be rendered as an opaque id.
    names: dict[str, str] = {}

    mean: float = 0.0
    worst: float = 0.0
    best: float = 0.0
    worst_traveler_id: str | None = None
    spread: float = 0.0

    # 0.6*mean + 0.4*worst. Sorting needs one number; this is the one that costs
    # an option something for ruining somebody.
    total: float = 0.0

    # How much of the intended evidence the underlying scores rested on. Four
    # people agreeing about a hotel nobody has any data for is not consensus,
    # and saying so is the difference between agreement and ignorance.
    coverage: float = 1.0

    @property
    def is_split(self) -> bool:
        """True when the group genuinely disagrees rather than being lukewarm."""
        return self.spread >= SPLIT_THRESHOLD

    @property
    def thinly_evidenced(self) -> bool:
        return self.coverage < 0.5

    def name_of(self, traveler_id: str | None) -> str:
        if traveler_id is None:
            return "someone"
        return self.names.get(traveler_id, traveler_id)

    def describe(self) -> str:
        """The score, rendered so the split cannot be dropped in the telling.

        There is deliberately no way to render this object that omits the
        disagreement when there is one. A caller wanting a bare number has to
        reach past `describe()` for `total`, which is a visible choice rather
        than an accident.
        """
        if not self.per_traveler:
            return "no traveler preferences to score against"

        if len(self.per_traveler) == 1:
            only = next(iter(self.per_traveler))
            return f"{self.per_traveler[only]:.2f} for {self.name_of(only)}"

        thin = (
            f" - though on only {self.coverage:.0%} of the usual evidence"
            if self.thinly_evidenced
            else ""
        )

        if not self.is_split:
            # "Agrees" is a claim about the group. With almost no data it is a
            # claim about our ignorance instead, and is worded as one.
            verb = "nobody has much to go on" if self.thinly_evidenced else "the group agrees"
            return f"{self.total:.2f}, and {verb} ({self.spread:.2f} apart){thin}"

        worst_name = self.name_of(self.worst_traveler_id)
        return (
            f"{self.total:.2f}, but the group is split: {self.best:.2f} at best against "
            f"{self.worst:.2f} for {worst_name}{thin}"
        )

    def ranked_travelers(self) -> list[tuple[str, float]]:
        """Every traveler and their score, unhappiest first."""
        return sorted(self.per_traveler.items(), key=lambda pair: (pair[1], pair[0]))


ConflictKind = Literal[
    "dietary",
    "accessibility",
    "budget",
    "pace",
    "airline",
    "interest",
    "importance",
    "schedule",
]

# blocking - the trip cannot honestly be called ready until someone decides
# material - a real trade-off the group should be told about
# minor    - worth a sentence, not worth a decision
ConflictSeverity = Literal["blocking", "material", "minor"]


class PreferenceConflict(BaseModel):
    """A disagreement, recorded per person so it cannot be averaged.

    Derived from preferences at read time rather than stored, so it can never
    drift out of step with the preferences behind it - the same reasoning
    `signals_from_evidence` uses. What *is* stored is the OpenQuestion a
    blocking conflict raises, because an answer has to persist.
    """

    # Derived from the content, never random. A conflict is recomputed from
    # scratch on every read, so a fresh id each time would mean an answer
    # recorded against one could never be matched to the next derivation of the
    # same disagreement - the group would settle something and be asked again.
    conflict_id: str = ""

    kind: ConflictKind
    severity: ConflictSeverity

    traveler_ids: list[str] = []

    summary: str

    # traveler_id -> what that person wants, in their own terms. The anti-
    # averaging device: there is no form of this record in which the two sides
    # have already been merged into one number.
    positions: dict[str, str] = {}

    # Decision names, item_ids or dates this bears on.
    affects: list[str] = []

    # Ways out. It proposes; the group decides. Never applied automatically.
    resolution_options: list[str] = []

    detected_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _identify(self) -> "PreferenceConflict":
        if not self.conflict_id:
            seed = "|".join(
                [
                    self.kind,
                    ",".join(sorted(self.traveler_ids)),
                    ",".join(sorted(self.affects)),
                ]
            )
            digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
            object.__setattr__(self, "conflict_id", f"cft_{digest}")
        return self

    @property
    def blocking(self) -> bool:
        return self.severity == "blocking"

    def question(self) -> str:
        """How this reads when it becomes something the group must answer."""
        return f"{self.summary} " + (
            f"Options: {'; '.join(self.resolution_options)}."
            if self.resolution_options
            else "How would you like to handle it?"
        )


# What a place is verified to support, from a provider that only ever asserts
# the positive case.
#
#   yes     - the provider positively attested it
#   unknown - it did not, which is NOT the same as "no"
#
# Google returns `servesVegetarianFood: false` for every pizzeria in Shibuya,
# and every pizzeria serves a margherita. Treating that false as a confirmed
# violation would hard-filter most of a city off a vegetarian's trip on the
# strength of a field that only ever means "not attested".
DietaryFit = Literal["yes", "unknown"]
