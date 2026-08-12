"""What this system claimed, what actually happened, and how wrong it usually is.

The rest of the codebase is careful about saying how much it knows: `coverage`
reports the share of a score that had data behind it, `Attestation` keeps "not
stated" apart from "no", a norm may never speak about a Tuesday. None of that
ever asked the other question - **were the numbers right?**

Three records, and the difference between them carries the design:

  * A `Prediction` is **derived from the trip, never stored**. Every checkable
    figure is already in `TripState` with its own provenance - a hotel area's
    `mean_minutes` beside its `travel_mode`, an airport's
    `ground_travel_minutes` beside its `ground_travel_source`. Deriving them
    means no new write path, every existing trip is covered retroactively, a
    prediction can never drift from the state it describes, and predictions
    cannot outlive their trip because they were never anywhere else.
  * An `Outcome` is a **stored fact**: what happened, checked by the archive,
    by a later look at the provider, or by the traveller. It is the whole
    corpus, so it denormalizes what calibration needs and carries **no
    location finer than `scope`** and no trip id - it has to outlive the trip
    that produced it, and it must not smuggle a travel history along with it.
  * A `Calibration` is **derived on every read**, content-hash identity, in
    the `PreferenceHypothesis` / `CommunitySignal` mould.

An outcome is always an interval, never a point. The archive answers exactly
and gives a zero-width one; a traveller answers "a bit longer than that" and
gives a wide one. Asking somebody for a number they never measured produces
invented precision wearing a decimal point, which is the thing this project
keeps refusing to do.
"""

import hashlib
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.models.common import new_id, utcnow

# The closed set of things this system will judge itself on. Closed because
# every dimension must name **how it gets checked** - a dimension with no
# checker is a claim that can never be wrong, which is the same defect as M9's
# preference that influences nothing, one level up.
Dimension = Literal[
    "travel_minutes",
    "hotel_headline_gap",
    "hours_shelf_life",
    "day_high_c",
]

# Who can contradict the claim.
#   same_fetch       - both numbers arrive together; checkable at once
#   archive          - an authoritative record of what happened
#   provider_recheck - ask the provider again later; measures going stale
#   traveller        - only a person who was there knows
CheckedBy = Literal["same_fetch", "archive", "provider_recheck", "traveller"]

# How much of the corpus stands behind an answer. Ordinal, and the sample
# count travels with it - "provisional" from 6 checks and "provisional" from
# 11 are not the same claim.
CalibrationStatus = Literal["uncalibrated", "provisional", "calibrated"]

# Which rung of the backoff chain answered. Reported, never hidden: a bias
# borrowed from a provider's global record is a weaker thing than one measured
# in the place being asked about.
CalibrationLevel = Literal["scoped", "mode", "provider", "none"]


class DimensionEntry(BaseModel):
    """One judgeable figure, and - non-negotiably - who checks it."""

    unit: str
    label: str
    checked_by: CheckedBy
    # Prose: what actually performs the check. The `CatalogueEntry.consumer`
    # discipline, pointed the other way.
    checker: str
    # Present only for traveller-checked dimensions: how to ask, with `{what}`
    # and `{value}` filled from the prediction's subject.
    question: str | None = None


DIMENSIONS: dict[Dimension, DimensionEntry] = {
    "travel_minutes": DimensionEntry(
        unit="minutes",
        label="travel time",
        checked_by="traveller",
        checker="the post-trip reflection asks about the estimates that drove a chosen option",
        question="We said {what} was about {value} minutes. How was it really?",
    ),
    "hotel_headline_gap": DimensionEntry(
        unit="currency",
        label="advertised nightly rate",
        checked_by="same_fetch",
        checker="`HotelOptionData.headline_gap()` - the advertised rate and the "
        "cheapest attributable quote arrive in the same fetch, so the claim is "
        "checkable the moment it is made",
    ),
    "hours_shelf_life": DimensionEntry(
        unit="days",
        label="opening hours",
        checked_by="provider_recheck",
        checker="asking Google Places again later and seeing whether the hours moved",
    ),
    "day_high_c": DimensionEntry(
        unit="celsius",
        label="forecast high",
        checked_by="archive",
        checker="Open-Meteo's archive for the date the forecast was about. Forecasts "
        "only: a historical norm is a claim about a season and checking it against "
        "one Tuesday is the category error `app/models/weather.py` exists to prevent",
    ),
}


class Prediction(BaseModel):
    """A checkable figure this system put in front of someone.

    Derived from `TripState`, never stored. `prediction_id` is a content hash
    so that an outcome recorded today still matches the same prediction
    derived tomorrow - the `PreferenceHypothesis._identify` mould, and the
    reason nothing has to be written down for the two halves to find each
    other.
    """

    prediction_id: str = ""

    trip_id: str

    provider: str
    dimension: Dimension
    # "transit", "driving", "walking" - or None where the dimension has no
    # mode. Part of the identity *and* of the calibration key: a transit
    # estimate and a driving one are not the same measurement, and
    # `hotel_area_service` silently falls back from one to the other.
    mode: str | None = None
    # Region-coarse, on purpose: this is the only piece of "where" allowed to
    # survive into the durable corpus.
    scope: str = "unknown"

    value: float
    # Stable name for the thing measured, e.g. "hotel_area:Ginza". Identity
    # rests on it, and the traveller's question is phrased from it.
    subject: str
    # Human phrasing of the same thing, for the question and the evidence line.
    subject_label: str = ""

    # Which decision this figure argued for, and whether that option won.
    # Only a figure that decided something is worth a traveller's attention.
    decision: str | None = None
    drove_the_choice: bool = False

    observed_at: datetime | None = None
    applies_to: date | None = None

    extra: dict[str, Any] = {}

    @model_validator(mode="after")
    def _identify(self) -> "Prediction":
        if not self.prediction_id:
            seed = "|".join(
                [self.trip_id, self.provider, self.dimension, self.mode or "", self.subject]
            )
            digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
            object.__setattr__(self, "prediction_id", f"pred_{digest}")
        return self


class Outcome(BaseModel):
    """What actually happened, as an interval. Stored, and it outlives its trip.

    Everything calibration needs is denormalized here, because the prediction
    is derived from a trip that may be deleted and the corpus must survive it -
    the reason `learning_signals` carries no FK either. What is deliberately
    *not* here: any trip id, and any location finer than `scope`. A durable
    table keyed to a person's movements is not a thing to create by accident
    while trying to measure a routing API.
    """

    outcome_id: str = Field(default_factory=lambda: new_id("out"))

    prediction_id: str

    provider: str
    dimension: Dimension
    mode: str | None = None
    scope: str = "unknown"

    predicted: float
    # The interval reality turned out to be in. An exact answer is a
    # zero-width one, so there is one shape and one arithmetic.
    actual_low: float
    actual_high: float

    checked_by: CheckedBy
    # What the traveller pressed, when a person was the checker. Kept so the
    # evidence line reads back in their words rather than as a number.
    answer: str | None = None

    observed_at: datetime = Field(default_factory=utcnow)

    @property
    def actual_mid(self) -> float:
        return (self.actual_low + self.actual_high) / 2

    @property
    def relative_error(self) -> tuple[float, float]:
        """Signed, as a fraction of what was predicted. (low, high).

        Positive means reality was larger than the claim - the estimate ran
        low. Relative rather than absolute so a 40-minute journey and a
        4-minute one can sit in the same corpus.
        """
        if self.predicted == 0:
            return (0.0, 0.0)
        return (
            (self.actual_low - self.predicted) / self.predicted,
            (self.actual_high - self.predicted) / self.predicted,
        )


class Calibration(BaseModel):
    """How wrong this provider usually is, here, at this. Derived on every read.

    Never stored, for the reason no derived record in this codebase is: an
    interpretation kept beside the facts drifts from them. `level` says which
    rung of the backoff chain answered and `sample_count` says how much stood
    behind it, because "borrowed from this provider's global record over 6
    checks" and "measured here over 40" are different claims and a single
    percentage would hide which one you were reading.
    """

    calibration_id: str = ""

    provider: str
    dimension: Dimension
    mode: str | None = None
    # The scope actually asked about.
    scope: str = "unknown"
    # The scope the answer came from - "" once the chain fell back past it.
    scope_used: str = ""

    level: CalibrationLevel = "none"
    status: CalibrationStatus = "uncalibrated"
    sample_count: int = 0

    # Median signed relative error, and a spread that errs wide. Both None
    # while `status == "uncalibrated"`: a bias is a claim, and this file does
    # not make claims it cannot support.
    bias: float | None = None
    spread: float | None = None

    @model_validator(mode="after")
    def _identify(self) -> "Calibration":
        if not self.calibration_id:
            seed = "|".join(
                [self.provider, self.dimension, self.mode or "", self.scope, self.level]
            )
            digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
            object.__setattr__(self, "calibration_id", f"cal_{digest}")
        return self

    @property
    def is_usable(self) -> bool:
        return self.bias is not None and self.spread is not None


class CalibratedEstimate(BaseModel):
    """A figure, and what the record says it is more likely to be.

    `raw` is never touched. The stored 14 minutes stays 14 minutes for ever -
    it is what the provider said and what the provenance points at. This adds
    a reading of it, and says how it was arrived at.
    """

    raw: float
    low: float | None = None
    high: float | None = None
    calibration: Calibration

    @property
    def adjusted(self) -> bool:
        return self.low is not None and self.high is not None
