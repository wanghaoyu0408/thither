"""The shapes a trip preview comes back in. Derived on every read, never stored.

A forecast here is not a prediction of what will happen. It is the plan's own
figures and this system's explicit assumptions, propagated through each day at
their low and high ends, so the fragile days show themselves before anyone is
standing in them. Three vocabularies carry the honesty:

  * **Provenance** — every input names what kind of number it is. A measured
    drive and a guessed parking buffer must never look alike on screen, and an
    unknown is a named absence, not a zero.
  * **Scenario** — optimistic / expected / conservative. Each is an interval,
    not a point, because the assumptions inside them refuse to be points.
  * **Verdict** — four words, no scores. "87% feasible" would be fake
    precision wearing a percent sign, and this codebase does not do that.
"""

from datetime import date as date_type
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import utcnow

# What kind of number an input is. "measured" came from a provider for this
# exact leg; "calibrated" is measured plus an earned error band (M10, and only
# a `calibrated`-status record qualifies - provisional evidence may be shown
# but never moves an output); "assumption" is this system's own stated guess;
# "unknown" is a named absence that must never be folded in as zero.
Provenance = Literal["measured", "calibrated", "assumption", "unknown"]

Scenario = Literal["optimistic", "expected", "conservative"]

SCENARIOS: tuple[Scenario, ...] = ("optimistic", "expected", "conservative")

# The closed set of things a preview may find. Closed for the same reason
# every other vocabulary here is: an open set would mint finding types no
# renderer knows and no test pins.
FindingKind = Literal[
    "late_arrival_risk",
    "tight_buffer",
    "weather_exposure",
    "parking_uncertainty",
    "excessive_travel",
    "pace_mismatch",
    "meal_gap",
    "sunset_risk",
    "unknown_dependency",
]

# The whole verdict vocabulary. A day is exactly one of these.
Verdict = Literal["comfortable", "workable", "fragile", "blocking"]


class Window(BaseModel):
    """A span of wall-clock the plan might actually be at, low to high.

    Naive local time at the destination, like every itinerary time. A window
    is the honest shape of a propagated arrival: the inputs are intervals, so
    the output cannot be a point without lying about one end.
    """

    low: datetime
    high: datetime

    def label(self) -> str:
        if self.low == self.high:
            return f"{self.low:%H:%M}"
        return f"{self.low:%H:%M}–{self.high:%H:%M}"


class Estimate(BaseModel):
    """One input to the propagation, with its provenance on its sleeve.

    `label` is screen-ready prose ("drive · 22 min provider figure") - no
    identifiers, no developer shorthand (ledger 60). `low`/`high` are None
    exactly when `provenance == "unknown"`, and an unknown contributes no
    minutes anywhere - it flags the chain instead.
    """

    what: str
    label: str
    low: float | None = None
    high: float | None = None
    unit: str = "minutes"
    provenance: Provenance


class StopForecast(BaseModel):
    """One itinerary item, with where the day might actually be when it starts."""

    item_id: str
    title: str
    item_type: str

    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None

    # A commitment rather than a preference: fixed time, a reservation, or a
    # lock. Only a committed stop can be "missed" - being late to a stroll is
    # a schedule sliding, not a promise broken.
    committed: bool = False

    # Arrival windows per scenario. Empty for the first timed stop (nothing
    # travels into it) and for untimed items.
    arrival: dict[Scenario, Window] = {}
    departure: dict[Scenario, Window] = {}

    # The figures and guesses that produced the windows, provenance included.
    inputs: list[Estimate] = []

    # True from the first unmeasured entity-to-entity leg onward: every window
    # after that point assumes the schedule held where the data ran out, and
    # no lateness finding is ever derived from an assumed schedule.
    rests_on_unknown: bool = False


class SimulationFinding(BaseModel):
    """One way this day could crack, with the evidence that says so.

    `breaks` marks a finding that snaps a real commitment in the conservative
    scenario (or is forecast-grade weather on an outdoor stop) - the ones that
    make a day fragile rather than merely imperfect. Everything else is
    context a traveller may well accept.
    """

    kind: FindingKind
    item_ids: list[str] = []
    message: str
    evidence: list[str] = []
    breaks: bool = False


class DayForecast(BaseModel):
    date: date_type
    verdict: Verdict

    stops: list[StopForecast] = []
    findings: list[SimulationFinding] = []

    # The DaySummary vocabulary, kept so "comfortable" can require that every
    # leg was actually measured rather than merely unremarkable.
    legs_total: int = 0
    legs_measured: int = 0


class TripForecast(BaseModel):
    """The whole preview. A pure function of the trip - recomputed, never kept.

    `revision` names the state this was computed from, so a stale forecast is
    detectable rather than quietly wrong.
    """

    trip_id: str
    revision: int

    days: list[DayForecast] = []

    generated_at: datetime = Field(default_factory=utcnow)

    @property
    def worst(self) -> Verdict:
        order: list[Verdict] = ["comfortable", "workable", "fragile", "blocking"]
        worst: Verdict = "comfortable"
        for day in self.days:
            if order.index(day.verdict) > order.index(worst):
                worst = day.verdict
        return worst
