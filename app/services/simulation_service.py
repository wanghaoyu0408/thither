"""Propagate the plan's own figures through each day and see where it cracks.

The validator answers "is this schedule consistent as written" - one scenario,
raw figures, hard rules. This module answers the question after that one: when
every figure wanders inside the width it is known to wander, which days stay
comfortable and which quietly depend on everything going right?

Not prediction. No randomness, no Monte Carlo, no model in the loop: known
facts and explicit assumptions, pushed through interval arithmetic that a
person could redo on paper. The language model may *explain* this output; it
never computes a minute of it.

Division of labour with the validator, deliberately one-directional:
`validate_itinerary` runs first and its result is an **input** here. Its
errors are what make a day `blocking`; its warnings fold into findings under
this module's names with their original messages as evidence; nothing is
recomputed against a second set of thresholds, so the two can never disagree
about a number. M10's rule survives intact: a calibration band may widen a
window or add a finding - it never clears a validator error.

Nothing is stored. A forecast is a pure function of
(state, travel, calibrations, settings), recomputed on every read - the
conflicts / hypotheses / calibrations mould.
"""

from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, time, timedelta

from app.config import Settings, get_settings
from app.models.itinerary import ItineraryDay, ItineraryItem
from app.models.simulation import (
    SCENARIOS,
    DayForecast,
    Estimate,
    Scenario,
    SimulationFinding,
    StopForecast,
    TripForecast,
    Verdict,
    Window,
)
from app.models.trip import TripState
from app.models.validation import ValidationIssue
from app.services.calibration_service import Calibrations
from app.services.day_metrics import _timed
from app.services.itinerary_service import _locked_item_ids
from app.services.option_metrics import ROUTES_PROVIDER
from app.services.validation_service import (
    TRANSFER_BUFFER_MINUTES,
    TravelLookup,
    _is_outdoor,
    _sun_local,
    long_haul_mode,
    mode_between,
    validate_itinerary,
)

# --- assumptions --------------------------------------------------------------


@dataclass(frozen=True)
class AssumptionEntry:
    """One stated guess, and - the M9/M10 discipline - who could override it.

    `label` reaches the screen, so it is a traveller's sentence, never
    developer shorthand (ledger 60). `overridable_by` names the resolved
    per-traveller preference field that should replace this default once the
    Travel Twin learns it; None means no such field exists today, and naming
    an unbuilt capability is the mistake ledger 44 exists to remember - the
    place a new key would be added is `learning_service.CATALOGUE`.
    """

    label: str
    low: float
    high: float
    unit: str = "minutes"
    overridable_by: str | None = None


ASSUMPTIONS: dict[str, AssumptionEntry] = {
    # How far a measured figure tends to wander per mode, as signed fractions,
    # used only when no *calibrated* record exists for the key (a provisional
    # record may be shown elsewhere; it never moves an output - M10).
    "driving_spread": AssumptionEntry(
        label="traffic", low=-0.10, high=0.25, unit="fraction"
    ),
    "transit_spread": AssumptionEntry(
        label="a missed connection", low=0.0, high=0.30, unit="fraction"
    ),
    "walking_spread": AssumptionEntry(
        label="walking pace", low=-0.10, high=0.15, unit="fraction"
    ),
    # Finding somewhere to put the car and walking in from it, when nobody has
    # measured it. Applied per driven leg; a measured parking walk replaces it.
    "parking_buffer": AssumptionEntry(
        label="finding parking and walking in",
        low=5.0,
        high=15.0,
        overridable_by="pace.parking_sensitive",
    ),
    # Between reaching a hotel and actually being checked in.
    "checkin_buffer": AssumptionEntry(label="checking in", low=10.0, high=20.0),
}

# A stretch this long inside the meal window with nothing to eat is worth a
# line. 6 rather than 5 because the stock templates end lunch around 13:30 and
# start dinner at 19:00 - a 5.5h gap that is normal, not a finding; flagging
# every generated day would teach people to ignore the finding that matters.
MEAL_GAP_HOURS = 6.0
MEAL_WINDOW = (time(11, 0), time(21, 0))

# Validator warning types folded into findings, under this module's names.
# Folded, never recomputed: the evidence line is the validator's own message,
# so the two systems cannot disagree about a number.
_FOLDS: dict[str, tuple[str, bool]] = {
    # validator type -> (finding kind, breaks)
    "weather_rain_risk": ("weather_exposure", True),
    "weather_wind_risk": ("weather_exposure", True),
    "weather_seasonal_risk": ("weather_exposure", False),
    "daily_walking_exceeded": ("excessive_travel", False),
    "daily_transit_excessive": ("excessive_travel", False),
    "day_overloaded": ("pace_mismatch", False),
    "insufficient_transfer": ("tight_buffer", False),
    "tight_against_measured_history": ("tight_buffer", False),
    "parking_unavailable": ("parking_uncertainty", True),
    "parking_unverified": ("parking_uncertainty", False),
    "parking_access_time": ("tight_buffer", False),
}


def resolve_assumptions(state: TripState) -> dict[str, AssumptionEntry]:
    """The assumption set for this trip. Today: the defaults, unchanged.

    This function exists so that per-traveller learning has exactly one seam
    to arrive through. When the Travel Twin can support "this person needs
    twenty minutes for parking, not five", the accepted value reaches the
    resolved preference snapshot, and this is the function that reads it -
    every consumer of an assumption already goes through here, so nothing
    else will need to change. It deliberately does no overriding yet: an
    override path that exists but is empty is a seam; one that half-works is
    a bug factory.
    """
    return ASSUMPTIONS


# --- interval arithmetic ------------------------------------------------------

Interval = tuple[float, float]


def _leg_scenarios(
    minutes: float, mode: str, assumptions: dict[str, AssumptionEntry], calibrations
) -> tuple[dict[Scenario, Interval], Estimate]:
    """One measured route figure, as three intervals and its evidence line.

    The endpoints come from an earned calibration band when one is
    `calibrated`, else from the per-mode assumption spread. The figure itself
    stays the claim: expected is [m, m], optimistic reaches down to the low
    end, conservative up to the high end - so the scenarios bracket the claim
    rather than replace it.
    """
    low = high = minutes
    provenance = "measured"
    note = "provider figure"

    band = (
        calibrations.band(minutes, ROUTES_PROVIDER, "travel_minutes", mode)
        if calibrations is not None
        else None
    )
    if band is not None and band.adjusted and band.calibration.status == "calibrated":
        low, high = band.low, band.high
        provenance = "calibrated"
        note = f"checked record: 8 in 10 ran {low:.0f}–{high:.0f} min"
    else:
        entry = assumptions.get(f"{mode}_spread") or assumptions["transit_spread"]
        low = minutes * (1 + entry.low)
        high = minutes * (1 + entry.high)

    scenarios: dict[Scenario, Interval] = {
        "optimistic": (min(low, minutes), minutes),
        "expected": (minutes, minutes),
        "conservative": (minutes, max(high, minutes)),
    }
    estimate = Estimate(
        what="route",
        label=f"{mode} · {minutes:.0f} min · {note}",
        low=round(min(low, minutes), 1),
        high=round(max(high, minutes), 1),
        provenance=provenance,
    )
    return scenarios, estimate


def _assumption_scenarios(entry: AssumptionEntry, what: str) -> tuple[dict[Scenario, Interval], Estimate]:
    """A stated guess: expected keeps the whole band, refusing to pick a point."""
    scenarios: dict[Scenario, Interval] = {
        "optimistic": (entry.low, entry.low),
        "expected": (entry.low, entry.high),
        "conservative": (entry.high, entry.high),
    }
    estimate = Estimate(
        what=what,
        label=f"{entry.label} · {entry.low:.0f}–{entry.high:.0f} min · assumption",
        low=entry.low,
        high=entry.high,
        provenance="assumption",
    )
    return scenarios, estimate


def _measured_scenarios(minutes: float, what: str, label: str) -> tuple[dict[Scenario, Interval], Estimate]:
    """A measured non-route figure (a parking walk): the same in every scenario."""
    point: Interval = (minutes, minutes)
    scenarios: dict[Scenario, Interval] = {s: point for s in SCENARIOS}
    estimate = Estimate(
        what=what,
        label=f"{label} · {minutes:.0f} min · measured",
        low=minutes,
        high=minutes,
        provenance="measured",
    )
    return scenarios, estimate


def _add(a: dict[Scenario, Interval], b: dict[Scenario, Interval]) -> dict[Scenario, Interval]:
    return {s: (a[s][0] + b[s][0], a[s][1] + b[s][1]) for s in SCENARIOS}


_ZERO: dict[Scenario, Interval] = {s: (0.0, 0.0) for s in SCENARIOS}


# --- propagation --------------------------------------------------------------


def _committed(item: ItineraryItem, locked: set[str]) -> bool:
    return bool(
        item.time_flexibility == "fixed"
        or item.reservation_required
        or item.reservation_booked
        or item.status == "locked"
        or item.item_id in locked
    )


def _window(base: datetime, interval: Interval) -> Window:
    return Window(
        low=base + timedelta(minutes=interval[0]),
        high=base + timedelta(minutes=interval[1]),
    )


def _propagate_day(
    day: ItineraryDay,
    state: TripState,
    travel: TravelLookup,
    assumptions: dict[str, AssumptionEntry],
    calibrations,
    locked: set[str],
) -> tuple[list[StopForecast], list[SimulationFinding], int, int]:
    """Walk the day's timed items, carrying an arrival interval per scenario.

    The rules, stated once:

      * previous departure + route + access buffers = arrival window;
      * actual start = max(scheduled start, arrival) - being early means
        waiting, not starting sooner;
      * a flexible item keeps its written duration, so lateness cascades; a
        fixed one ends when it ends, so lateness eats the visit instead;
      * an unmeasured entity-to-entity leg advances nothing. The chain resets
        to the schedule, everything downstream is flagged `rests_on_unknown`,
        and no lateness finding is ever derived from an assumed schedule -
        absence is not a zero.
    """
    ordered = _timed(day.items)
    stops: list[StopForecast] = []
    findings: list[SimulationFinding] = []
    legs_total = 0
    legs_measured = 0

    driving_trip = long_haul_mode(state) == "driving"
    rests_on_unknown = False
    prev_item: ItineraryItem | None = None

    # Where the day "is", per scenario, as (low, high) datetimes - the
    # departure window of the previous stop.
    cursor: dict[Scenario, tuple[datetime, datetime]] | None = None

    for item in ordered:
        stop = StopForecast(
            item_id=item.item_id,
            title=item.title,
            item_type=item.type,
            scheduled_start=item.start_at,
            scheduled_end=item.end_at,
            committed=_committed(item, locked),
        )

        if cursor is None:
            # The first timed stop anchors the day: nothing travels into it.
            end = item.end_at or item.start_at
            cursor = {s: (end, end) for s in SCENARIOS}
            stop.rests_on_unknown = rests_on_unknown
            stops.append(stop)
            prev_item = item
            continue

        # --- the leg in ------------------------------------------------------
        totals = dict(_ZERO)
        inputs: list[Estimate] = []
        leg_unknown = False

        origin = state.entities.get(prev_item.entity_id) if prev_item.entity_id else None
        destination = state.entities.get(item.entity_id) if item.entity_id else None

        if prev_item.entity_id and item.entity_id:
            legs_total += 1
            mode = mode_between(state, origin, destination)
            minutes = travel.duration(prev_item.entity_id, item.entity_id, mode)
            if minutes is None:
                # Nobody measured this journey. It contributes no minutes -
                # not zero minutes - and poisons every claim downstream.
                leg_unknown = True
                inputs.append(
                    Estimate(
                        what="route",
                        label=f"{mode} to {item.title} · unknown · not looked up",
                        provenance="unknown",
                    )
                )
            else:
                legs_measured += 1
                leg, estimate = _leg_scenarios(minutes, mode, assumptions, calibrations)
                totals = _add(totals, leg)
                inputs.append(estimate)

                if mode == "driving" and driving_trip:
                    arrival_ctx = state.arrival.get(item.entity_id)
                    overhead = arrival_ctx.overhead_minutes if arrival_ctx else None
                    if overhead is not None:
                        parking, estimate = _measured_scenarios(
                            overhead, "parking", "parking walk"
                        )
                    else:
                        parking, estimate = _assumption_scenarios(
                            assumptions["parking_buffer"], "parking"
                        )
                        findings.append(
                            SimulationFinding(
                                kind="parking_uncertainty",
                                item_ids=[item.item_id],
                                message=(
                                    f"parking at {item.title} is unverified, so "
                                    f"{assumptions['parking_buffer'].low:.0f}–"
                                    f"{assumptions['parking_buffer'].high:.0f} min is assumed"
                                ),
                                evidence=[estimate.label],
                            )
                        )
                    totals = _add(totals, parking)
                    inputs.append(estimate)

        if item.type == "hotel":
            checkin, estimate = _assumption_scenarios(assumptions["checkin_buffer"], "checkin")
            totals = _add(totals, checkin)
            inputs.append(estimate)

        # --- the windows ------------------------------------------------------
        if leg_unknown:
            rests_on_unknown = True
            # Reset to the schedule: from here on the windows say "if the plan
            # held where the data ran out", and the flag says exactly that.
            start_anchor = item.start_at
            cursor = {s: (start_anchor, start_anchor) for s in SCENARIOS}
        else:
            arrival: dict[Scenario, Window] = {}
            departure: dict[Scenario, Window] = {}
            new_cursor: dict[Scenario, tuple[datetime, datetime]] = {}
            duration = (
                (item.end_at - item.start_at) if item.end_at and item.start_at else timedelta()
            )
            for s in SCENARIOS:
                lo = cursor[s][0] + timedelta(minutes=totals[s][0])
                hi = cursor[s][1] + timedelta(minutes=totals[s][1])
                arrival[s] = Window(low=lo, high=hi)
                # Early means waiting; late means starting late.
                start_lo = max(item.start_at, lo)
                start_hi = max(item.start_at, hi)
                if item.time_flexibility == "fixed" and item.end_at:
                    # The reservation ends when it ends; lateness eats the
                    # visit rather than pushing the rest of the day.
                    end_lo = end_hi = item.end_at
                else:
                    end_lo = start_lo + duration
                    end_hi = start_hi + duration
                departure[s] = Window(low=end_lo, high=end_hi)
                new_cursor[s] = (end_lo, end_hi)
            stop.arrival = arrival
            stop.departure = departure
            cursor = new_cursor

        stop.inputs = inputs
        stop.rests_on_unknown = rests_on_unknown
        stops.append(stop)

        # --- scenario findings ------------------------------------------------
        if not rests_on_unknown and stop.arrival and item.start_at:
            conservative = stop.arrival["conservative"]
            expected = stop.arrival["expected"]
            if stop.committed:
                if conservative.high > item.start_at:
                    misses_expected = expected.high > item.start_at
                    findings.append(
                        SimulationFinding(
                            kind="late_arrival_risk",
                            item_ids=[item.item_id],
                            message=(
                                f"{item.title} starts at {item.start_at:%H:%M}; "
                                f"conservative arrival is {conservative.label()}"
                                + (
                                    " — and even the expected window slips"
                                    if misses_expected
                                    else ""
                                )
                            ),
                            evidence=[e.label for e in inputs],
                            breaks=True,
                        )
                    )
                elif conservative.high > item.start_at - timedelta(
                    minutes=TRANSFER_BUFFER_MINUTES
                ):
                    findings.append(
                        SimulationFinding(
                            kind="tight_buffer",
                            item_ids=[item.item_id],
                            message=(
                                f"{item.title} makes it, but with under "
                                f"{TRANSFER_BUFFER_MINUTES} min to spare in the "
                                f"conservative case (arrives {conservative.label()})"
                            ),
                            evidence=[e.label for e in inputs],
                        )
                    )

        prev_item = item

    if rests_on_unknown:
        unknowns = [
            estimate.label
            for stop in stops
            for estimate in stop.inputs
            if estimate.provenance == "unknown"
        ]
        findings.append(
            SimulationFinding(
                kind="unknown_dependency",
                item_ids=[stop.item_id for stop in stops if stop.rests_on_unknown],
                message=(
                    "part of this day rests on journeys nobody has measured — "
                    "the windows there assume the schedule holds"
                ),
                evidence=unknowns,
            )
        )

    return stops, findings, legs_total, legs_measured


# --- statics: sunset, meals ---------------------------------------------------


def _sunset_findings(
    day: ItineraryDay, state: TripState, stops: list[StopForecast]
) -> list[SimulationFinding]:
    """Outdoor stops that may still be running after dark.

    The validator's own sunset check reads item *names* and errors on a
    "Sunset viewpoint" scheduled after sunset; this is the broader, softer
    claim - a park is a different place at night whatever it is called. Uses
    the conservative departure when propagation produced one, the written end
    otherwise; never fires from an assumed schedule.
    """
    weather = day.weather
    if weather is None:
        return []
    sunset = _sun_local(weather.sunset, state)
    if sunset is None:
        return []

    by_id = {stop.item_id: stop for stop in stops}
    findings: list[SimulationFinding] = []
    for item in day.items:
        if not _is_outdoor(state, item) or not item.end_at:
            continue
        stop = by_id.get(item.item_id)
        if stop is not None and stop.rests_on_unknown:
            continue
        end = (
            stop.departure["conservative"].high
            if stop is not None and stop.departure
            else item.end_at
        )
        if end.time() > sunset.time():
            findings.append(
                SimulationFinding(
                    kind="sunset_risk",
                    item_ids=[item.item_id],
                    message=(
                        f"{item.title} is outdoors and may run until {end:%H:%M}, "
                        f"after sunset at {sunset:%H:%M}"
                    ),
                    evidence=[f"sunset {sunset:%H:%M} · measured"],
                    breaks=True,
                )
            )
    return findings


def _meal_findings(day: ItineraryDay) -> list[SimulationFinding]:
    """A long stretch of a planned day with nothing to eat in it.

    New ground: nothing else in the codebase looks at meal spacing. Only the
    scheduled span is judged - an empty afternoon is free time, not a missed
    lunch - and only when the day is substantial enough for the gap to mean
    anything.
    """
    ordered = _timed(day.items)
    if len(ordered) < 2:
        return []

    span_start = max(
        datetime.combine(day.date, MEAL_WINDOW[0]), ordered[0].start_at
    )
    span_end = min(
        datetime.combine(day.date, MEAL_WINDOW[1]),
        max(item.end_at or item.start_at for item in ordered),
    )
    if span_end <= span_start:
        return []

    meals = sorted(
        (
            (item.start_at, item.end_at or item.start_at)
            for item in ordered
            if item.type == "restaurant"
        ),
        key=lambda pair: pair[0],
    )

    # The longest stretch inside the span not touching a meal.
    gaps: list[tuple[datetime, datetime]] = []
    at = span_start
    for meal_start, meal_end in meals:
        if meal_start > at:
            gaps.append((at, min(meal_start, span_end)))
        at = max(at, meal_end)
    if at < span_end:
        gaps.append((at, span_end))

    findings: list[SimulationFinding] = []
    for gap_start, gap_end in gaps:
        hours = (gap_end - gap_start).total_seconds() / 3600.0
        if hours > MEAL_GAP_HOURS:
            findings.append(
                SimulationFinding(
                    kind="meal_gap",
                    message=(
                        f"nothing to eat between {gap_start:%H:%M} and {gap_end:%H:%M} "
                        f"({hours:.0f}h+ with stops scheduled)"
                    ),
                    evidence=[
                        f"meal window {MEAL_WINDOW[0]:%H:%M}–{MEAL_WINDOW[1]:%H:%M} · assumption"
                    ],
                )
            )
    return findings


# --- folding the validator ----------------------------------------------------


def _fold_issues(
    day: ItineraryDay, issues: list[ValidationIssue]
) -> tuple[list[SimulationFinding], bool]:
    """The validator's word on this day, folded in rather than recomputed.

    Errors make the day blocking outright. Warnings become findings under
    this module's names, carrying the validator's own message as evidence -
    one set of thresholds, one place they live.
    """
    item_ids = {item.item_id for item in day.items}
    todays = [
        issue
        for issue in issues
        if not issue.item_ids or any(item_id in item_ids for item_id in issue.item_ids)
    ]

    blocking = any(issue.severity == "error" for issue in todays)
    findings: list[SimulationFinding] = []
    for issue in todays:
        if issue.severity == "error":
            continue  # already decided the verdict; not re-dressed as a finding
        fold = _FOLDS.get(issue.type)
        if fold is None:
            continue
        kind, breaks = fold
        findings.append(
            SimulationFinding(
                kind=kind,  # type: ignore[arg-type]
                item_ids=issue.item_ids,
                message=issue.message,
                evidence=[f"validator: {issue.type}"],
                breaks=breaks,
            )
        )
    return findings, blocking


# --- the verdict --------------------------------------------------------------


def _verdict(
    findings: list[SimulationFinding],
    *,
    blocking: bool,
    rests_on_unknown: bool,
    all_measured: bool,
) -> Verdict:
    """Four words, one rule each. No arithmetic, no score.

    Unknowns cap a day at workable from both directions: a day resting on
    unmeasured journeys can never be called comfortable, and no `breaks`
    finding is ever derived from an assumed schedule, so it cannot be called
    fragile on invented evidence either.
    """
    if blocking:
        return "blocking"
    if any(finding.breaks for finding in findings):
        return "fragile"
    if findings or rests_on_unknown or not all_measured:
        return "workable"
    return "comfortable"


# --- entry point --------------------------------------------------------------


def simulate_trip(
    state: TripState,
    *,
    travel: TravelLookup | None = None,
    calibrations: Calibrations | None = None,
    issues: list[ValidationIssue] | None = None,
    settings: Settings | None = None,
    target_date: date_type | None = None,
) -> TripForecast:
    """The whole preview. Pure; writes nothing; safe to run any time.

    `issues` may be injected for tests; left None, the validator is invoked
    here - which keeps it the single authority on as-written feasibility, and
    makes it impossible to compute a forecast that forgot to consult it.
    """
    travel = travel or TravelLookup()
    settings = settings or get_settings()
    if issues is None:
        issues = validate_itinerary(
            state, travel=travel, calibrations=calibrations
        ).issues

    assumptions = resolve_assumptions(state)
    locked = _locked_item_ids(state)

    days: list[DayForecast] = []
    for day in state.itinerary.days:
        if target_date is not None and day.date != target_date:
            continue

        stops, findings, legs_total, legs_measured = _propagate_day(
            day, state, travel, assumptions, calibrations, locked
        )
        findings += _sunset_findings(day, state, stops)
        findings += _meal_findings(day)
        folded, blocking = _fold_issues(day, issues)
        findings += folded

        days.append(
            DayForecast(
                date=day.date,
                verdict=_verdict(
                    findings,
                    blocking=blocking,
                    rests_on_unknown=any(stop.rests_on_unknown for stop in stops),
                    all_measured=legs_measured == legs_total,
                ),
                stops=stops,
                findings=findings,
                legs_total=legs_total,
                legs_measured=legs_measured,
            )
        )

    return TripForecast(trip_id=state.trip_id, revision=state.revision, days=days)
