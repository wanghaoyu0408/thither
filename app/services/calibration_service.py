"""Claims out of the trip, outcomes in, a measured bias out. Pure.

Three moves, and the order matters:

  * `predictions_from(state)` reads every checkable figure straight out of
    `TripState`. Nothing is written to make this work. The figures were always
    there with their own provenance - `mean_minutes` beside `travel_mode`,
    `ground_travel_minutes` beside `ground_travel_source` - and a derived
    prediction cannot drift from the state that produced it, covers trips
    planned before this milestone existed, and cannot outlive its trip.
  * `outcome_for` turns an answer into an interval. Every answer, from the
    archive or from a person, becomes the same shape, so there is one
    arithmetic downstream.
  * `calibration_for` reads the corpus and reports how wrong this provider
    usually is - or, far more often, that there is not enough to say. The
    second is the answer this file is really built to give safely.

The arithmetic is stated here rather than hidden, in the manner of
`weather_service`'s climatology: bias is the **median** of the samples' signed
relative error, because one road closure must not become a finding, and the
spread deliberately errs wide - it adds the samples' own vagueness to their
disagreement. An interval that is too generous costs a traveller nothing. One
that is too tight is the confident wrongness this project exists to avoid.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from statistics import median

from app.config import Settings, get_settings
from app.models.calibration import (
    DIMENSIONS,
    CalibratedEstimate,
    Calibration,
    CalibrationLevel,
    CheckedBy,
    Dimension,
    Outcome,
    Prediction,
)
from app.models.decision import Decision, HotelAreaOption, HotelOptionData
from app.models.flight import AirportOption
from app.models.trip import TripState

# What the traveller can press, and the interval each answer means as a
# multiple of what was predicted. Bands rather than a number box: nobody timed
# their walk, and a figure typed from memory is invented precision with a
# person's name on it. "About right" is deliberately the widest kind of
# agreement - somebody who says a 14-minute estimate was about right is not
# claiming it was 14.
ANSWER_BANDS: dict[str, tuple[float, float]] = {
    "about_right": (0.85, 1.15),
    "a_bit_longer": (1.15, 1.5),
    "much_longer": (1.5, 2.2),
    "quicker": (0.6, 0.9),
}

ANSWER_WORDS: dict[str, str] = {
    "about_right": "about right",
    "a_bit_longer": "a bit longer",
    "much_longer": "much longer",
    "quicker": "quicker than that",
}

# An exact figure is an interval too, just a zero-width one - so the corpus
# never has to know which kind it is holding.
EXACT = "exact"

# How much of the corpus is left outside the reported band, at each end. The
# band claims eight checks in ten, and says so; the two in ten outside it are
# what `sample_count` is travelling with the figure to let a reader weigh.
BAND_TAIL = 0.1

# Google Routes is the only provider that measures ground travel here, and it
# only counts when it actually ran: `not_looked_up` and `unavailable` are
# absences, and an absent measurement is not a wrong one (invariant 1).
ROUTES_PROVIDER = "google_routes"
MEASURED_GROUND_SOURCE = "routes_api"


def scope_of(state: TripState) -> str:
    """Region-coarse, and the finest "where" allowed into the durable corpus.

    The IANA zone and nothing else. `destination.country` was the obvious
    second choice and is deliberately refused: it is free text the model
    fills in, and this database holds both "Japan" and "日本" for the same
    country - two buckets that would split the evidence and then quietly
    disagree with each other. A zone id has one spelling, comes from Places
    rather than from prose, and is the right granularity for the thing being
    measured, since whether a routing API has transit data is a fact about a
    country and not about a street.

    "unknown" is a real bucket and not a failure: the backoff chain drops to
    the mode and provider rungs, which is where the transit-versus-driving
    distinction lives anyway.
    """
    return state.brief.timezone or "unknown"


# --- predictions, derived ----------------------------------------------------


def _options_of(decision: Decision | None):
    if decision is None:
        return []
    return [
        (option, decision.selected_option_id == option.option_id)
        for option in decision.options
        if option.status != "rejected"
    ]


def predictions_from(state: TripState) -> list[Prediction]:
    """Every figure in this trip that reality could contradict.

    Only measurements: an airport whose drive time was never looked up has no
    claim to be wrong about, and a historical norm is not a claim about a date
    at all.
    """
    scope = scope_of(state)
    out: list[Prediction] = []

    def add(**fields) -> None:
        out.append(Prediction(trip_id=state.trip_id, scope=scope, **fields))

    for option, chosen in _options_of(state.decisions.hotel_area):
        data = option.data
        if not isinstance(data, HotelAreaOption) or data.mean_minutes is None:
            continue
        add(
            provider=ROUTES_PROVIDER,
            dimension="travel_minutes",
            mode=data.travel_mode,
            value=data.mean_minutes,
            subject=f"hotel_area:{data.area_name}",
            subject_label=f"getting around from {data.area_name}",
            decision="hotel_area",
            drove_the_choice=chosen,
        )

    for option, chosen in _options_of(state.decisions.hotel):
        data = option.data
        if not isinstance(data, HotelOptionData):
            continue
        mean = data.mean_route_minutes()
        if mean is not None:
            add(
                provider=ROUTES_PROVIDER,
                dimension="travel_minutes",
                mode=data.route_mode,
                value=mean,
                subject=f"hotel:{data.entity_id or data.name}",
                subject_label=f"getting to your places from {data.name}",
                decision="hotel",
                drove_the_choice=chosen,
            )
        # The advertised rate is a claim, and the cheapest quote anyone can be
        # named for arrives in the same fetch to contradict it (ledger 4). Both
        # halves are already here, so this one needs nobody's help to check.
        if data.headline_nightly is not None and data.cheapest_quote is not None:
            add(
                provider=data.provider,
                dimension="hotel_headline_gap",
                value=data.headline_nightly.amount,
                subject=f"headline:{data.entity_id or data.name}",
                subject_label=f"the advertised rate at {data.name}",
                decision="hotel",
                drove_the_choice=chosen,
                observed_at=data.observed_at,
            )

    for name in ("departure_airport", "arrival_airport"):
        for option, chosen in _options_of(getattr(state.decisions, name)):
            data = option.data
            if not isinstance(data, AirportOption):
                continue
            if (
                data.ground_travel_minutes is None
                or data.ground_travel_source != MEASURED_GROUND_SOURCE
            ):
                continue
            add(
                provider=ROUTES_PROVIDER,
                dimension="travel_minutes",
                mode="driving",
                value=data.ground_travel_minutes,
                subject=f"airport:{data.iata}",
                subject_label=f"the drive to {data.iata}",
                decision=name,
                drove_the_choice=chosen,
            )

    for day in state.itinerary.days:
        weather = day.weather
        # A norm describes a season and can never be wrong about a Tuesday.
        # Checking one against a single day's archive is exactly the category
        # error `app/models/weather.py` exists to prevent.
        if weather is None or not weather.is_forecast or weather.high_c is None:
            continue
        add(
            provider=weather.source or "unknown",
            dimension="day_high_c",
            value=weather.high_c,
            subject=f"weather:{day.date.isoformat()}",
            subject_label=f"the forecast high for {day.date.isoformat()}",
            observed_at=weather.observed_at,
            applies_to=day.date,
        )

    return out


# --- outcomes ----------------------------------------------------------------


def outcome_for(
    prediction: Prediction,
    *,
    answer: str,
    exact: float | None = None,
    checked_by: CheckedBy | None = None,
) -> Outcome | None:
    """One answer, as the interval it means. None when it means nothing.

    An exact figure wins over the chip: somebody who kept the receipt or timed
    the walk knows better than the band they also ticked.
    """
    if exact is not None:
        low = high = float(exact)
        answer_word = f"{exact:g} {DIMENSIONS[prediction.dimension].unit}"
    else:
        band = ANSWER_BANDS.get(answer)
        if band is None:
            return None
        low, high = (prediction.value * band[0], prediction.value * band[1])
        answer_word = ANSWER_WORDS.get(answer, answer)

    return Outcome(
        prediction_id=prediction.prediction_id,
        provider=prediction.provider,
        dimension=prediction.dimension,
        mode=prediction.mode,
        scope=prediction.scope,
        predicted=prediction.value,
        actual_low=low,
        actual_high=high,
        checked_by=checked_by or DIMENSIONS[prediction.dimension].checked_by,
        answer=answer_word,
    )


def outcome_from_measurement(
    prediction: Prediction, actual: float, *, checked_by: CheckedBy | None = None
) -> Outcome:
    """The archive, or a provider contradicting itself. Zero-width."""
    return Outcome(
        prediction_id=prediction.prediction_id,
        provider=prediction.provider,
        dimension=prediction.dimension,
        mode=prediction.mode,
        scope=prediction.scope,
        predicted=prediction.value,
        actual_low=actual,
        actual_high=actual,
        checked_by=checked_by or DIMENSIONS[prediction.dimension].checked_by,
        answer=f"{actual:g}",
    )


def automatic_outcomes(state: TripState) -> list[Outcome]:
    """The checks that need nobody - no person, no network, no waiting.

    Only one dimension qualifies today, and it qualifies completely: a hotel's
    advertised nightly rate and the cheapest rate any named booking site will
    actually honour arrive in the same fetch, so the claim and its refutation
    are already sitting beside each other in the trip. Ledger 4 found this by
    hand once - an advertised $70 that no listed site matched, all of them
    wanting $90 - and nothing has counted how often it happens since.

    Everything else needs a person who was there, an archive of what the
    weather did, or time to pass. Those live elsewhere on purpose: this
    function makes no network call and can be run over every trip in the
    database without asking anyone anything.
    """
    by_subject = {p.subject: p for p in predictions_from(state)}
    outcomes: list[Outcome] = []

    for option, _chosen in _options_of(state.decisions.hotel):
        data = option.data
        if not isinstance(data, HotelOptionData):
            continue
        quote = data.cheapest_quote
        if quote is None or quote.nightly is None:
            continue
        prediction = by_subject.get(f"headline:{data.entity_id or data.name}")
        if prediction is None:
            continue
        outcomes.append(
            outcome_from_measurement(prediction, quote.nightly.amount, checked_by="same_fetch")
        )

    return outcomes


# --- calibration, derived ----------------------------------------------------


def _matching(
    outcomes: Iterable[Outcome],
    *,
    provider: str,
    dimension: Dimension,
    mode: str | None = None,
    scope: str | None = None,
) -> list[Outcome]:
    return [
        o
        for o in outcomes
        if o.provider == provider
        and o.dimension == dimension
        and (mode is None or o.mode == mode)
        and (scope is None or o.scope == scope)
    ]


def _quantile(values: list[float], q: float) -> float:
    """Linear-interpolated quantile. Small samples clamp to their extremes
    rather than pretend to resolve a tail they do not have."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _bias_and_band(samples: list[Outcome]) -> tuple[float, float, float]:
    """The median error, and the band eight checks in ten actually landed in.

    Quantiles of the observed errors, not a spread computed around the median.
    The first version used median absolute deviation and the live corpus
    showed why that is unsafe here: half of thirty advertised hotel rates were
    matched exactly, so the deviation collapsed to ±1.6% and an advertised
    $200 was reported as "more likely $197-$203" - while three of those same
    thirty were understated by 13%, 20% and 67%. A tight interval wrapped
    around a fat tail is a more confident lie than no interval at all.

    Asymmetric because reality is: an advertised price is understated far more
    often than overstated, and forcing ± around a midpoint would invent a
    symmetry the data does not have. Each sample is itself an interval, so the
    low edge is taken over the samples' low ends and the high edge over their
    high ends - which folds in how vague each answer was without counting it
    twice.

    The tail outside the band is real and is not hidden: `sample_count`
    travels with every figure, and eight in ten is what the band claims.
    """
    bands = [o.relative_error for o in samples]
    bias = median([(low + high) / 2 for low, high in bands])
    return (
        bias,
        _quantile([low for low, _ in bands], BAND_TAIL),
        _quantile([high for _, high in bands], 1 - BAND_TAIL),
    )


def calibration_for(
    outcomes: Iterable[Outcome],
    *,
    provider: str,
    dimension: Dimension,
    mode: str | None = None,
    scope: str = "unknown",
    settings: Settings,
) -> Calibration:
    """How wrong this provider usually is here - or that we cannot say.

    The chain gives up specificity for evidence, one rung at a time, and says
    which rung answered. A bias borrowed from a provider's global record is a
    weaker claim than one measured in the place being asked about, and a
    reader who is not told which they are holding has been misled by omission.
    """
    corpus = list(outcomes)
    ladder: list[tuple[CalibrationLevel, list[Outcome], str]] = [
        (
            "scoped",
            _matching(corpus, provider=provider, dimension=dimension, mode=mode, scope=scope),
            scope,
        ),
        ("mode", _matching(corpus, provider=provider, dimension=dimension, mode=mode), ""),
        ("provider", _matching(corpus, provider=provider, dimension=dimension), ""),
    ]

    for level, samples, scope_used in ladder:
        if len(samples) < settings.calibration_min_samples:
            continue
        bias, low_error, high_error = _bias_and_band(samples)
        return Calibration(
            provider=provider,
            dimension=dimension,
            mode=mode,
            scope=scope,
            scope_used=scope_used,
            level=level,
            status=(
                "calibrated"
                if len(samples) >= settings.calibration_confident_samples
                else "provisional"
            ),
            sample_count=len(samples),
            bias=bias,
            low_error=low_error,
            high_error=high_error,
        )

    # Nothing stood up. Report the widest count we have so the surface can say
    # "checked twice, which is not enough" rather than fall silent - absence
    # is not negation, applied to this system's own track record.
    widest = _matching(corpus, provider=provider, dimension=dimension)
    return Calibration(
        provider=provider,
        dimension=dimension,
        mode=mode,
        scope=scope,
        level="none",
        status="uncalibrated",
        sample_count=len(widest),
    )


def calibrate(value: float, calibration: Calibration) -> CalibratedEstimate:
    """The figure, and the interval the record says it is more likely in.

    `raw` is never touched. The stored 14 minutes remains what the provider
    said and what the provenance points at; this is a reading of it.
    """
    if not calibration.is_usable:
        return CalibratedEstimate(raw=value, calibration=calibration)
    return CalibratedEstimate(
        raw=value,
        low=max(0.0, value * (1 + (calibration.low_error or 0.0))),
        high=max(0.0, value * (1 + (calibration.high_error or 0.0))),
        calibration=calibration,
    )


# There is deliberately no function here that moves a figure onto a calibrated
# basis for ranking.
#
# The plan called for one: `hotel_area_service` falls back from transit to
# driving where Google publishes no transit data (ledger 2), so two areas'
# `mean_minutes` looked like they could be measured in different units and
# ranked against each other anyway. Checked against the live database, that
# never happens - twelve stored shortlists carry a travel mode and none of
# them mixes two, because a shortlist is measured in a single route matrix and
# the fallback applies to the whole matrix at once.
#
# So every option on a card shares a bias, correcting them all would multiply
# them by the same number, and no order would change. A transform that
# provably cannot alter its output is not neutral: it is a place for a future
# reader to believe something is happening. Calibration annotates a card and
# widens a feasibility warning. It does not reorder anything, and
# `test_calibration_annotates_a_card_and_never_reorders_it` holds that shut.


# --- the corpus, ready to be asked -------------------------------------------

# How a band reads, per unit. Minutes are whole minutes: a band of "16.3 to
# 21.7 min" claims a resolution the answers behind it never had.
_UNIT_FORMAT: dict[str, Callable[[float], str]] = {
    "minutes": lambda v: f"{v:.0f} min",
    "currency": lambda v: f"{v:,.0f}",
    "celsius": lambda v: f"{v:.0f}°C",
    "days": lambda v: f"{v:.0f} days",
}


def _format(value: float, unit: str) -> str:
    return _UNIT_FORMAT.get(unit, lambda v: f"{v:g}")(value)


async def calibrations_for(session, state: TripState) -> "Calibrations":
    """Load the corpus for a trip's place. One query, at the edge.

    Lives here rather than in each router so there is one answer to "which
    outcomes are relevant to this trip" - all of them, filtered by key when
    asked, because a provider's record elsewhere is what the backoff chain
    falls back to.
    """
    from app.db.repository import CalibrationRepository

    return Calibrations.of(
        await CalibrationRepository(session).list_for(),
        scope=scope_of(state),
        settings=get_settings(),
    )


@dataclass(frozen=True)
class Calibrations:
    """The corpus and the place being asked about, ready for any key.

    Built once where a session exists and passed down, so that the pure
    services stay pure and a card, its explanation and its ranking cannot
    disagree about how well a number is known. Absent, every consumer falls
    back to the raw figure and says nothing - which is what every caller did
    before this milestone and remains correct.
    """

    outcomes: tuple[Outcome, ...] = ()
    scope: str = "unknown"
    settings: Settings | None = None

    @classmethod
    def of(
        cls, outcomes: Iterable[Outcome], *, scope: str, settings: Settings
    ) -> "Calibrations":
        return cls(outcomes=tuple(outcomes), scope=scope, settings=settings)

    def for_(
        self, provider: str, dimension: Dimension, mode: str | None = None
    ) -> Calibration:
        return calibration_for(
            self.outcomes,
            provider=provider,
            dimension=dimension,
            mode=mode,
            scope=self.scope,
            settings=self.settings or Settings(),
        )

    def band(
        self, value: float, provider: str, dimension: Dimension, mode: str | None = None
    ) -> CalibratedEstimate:
        return calibrate(value, self.for_(provider, dimension, mode))

    def note(
        self, value: float, provider: str, dimension: Dimension, mode: str | None = None
    ) -> str:
        """What the record says about this figure, including that it says nothing.

        Never silent. A dimension nobody has ever checked and a dimension that
        has always been right look identical on a screen that renders nothing,
        and only one of them has earned the reader's trust.
        """
        estimate = self.band(value, provider, dimension, mode)
        record = estimate.calibration
        unit = DIMENSIONS[dimension].unit

        if not estimate.adjusted:
            if record.sample_count == 0:
                return "never checked against what actually happened"
            return (
                f"checked {record.sample_count} time"
                f"{'' if record.sample_count == 1 else 's'} so far — "
                "not enough to say how close this runs"
            )

        where = "here" if record.level == "scoped" else "elsewhere; none here yet"
        return (
            f"{record.sample_count} checks {where} — 8 in 10 landed "
            f"{_format(estimate.low, unit)}–{_format(estimate.high, unit)}"
        )


# --- what to ask a person ----------------------------------------------------


def questions_for(
    state: TripState,
    outcomes: Iterable[Outcome],
    *,
    settings: Settings,
    limit: int = 2,
    already_answered: set[str] | None = None,
) -> list[Prediction]:
    """The few estimates worth a traveller's attention after their trip.

    Rationed hard. A reflection card is not a survey, and the willingness to
    answer it is a budget that gets spent whether the questions were useful or
    not. So: only figures a person can check, only ones that argued for
    something they actually chose, and among those the ones whose calibration
    key knows least - asking again about a well-measured key buys nothing.
    """
    answered = already_answered or set()
    corpus = list(outcomes)

    candidates = [
        p
        for p in predictions_from(state)
        if DIMENSIONS[p.dimension].checked_by == "traveller"
        and p.drove_the_choice
        and p.prediction_id not in answered
    ]

    def known(p: Prediction) -> tuple[int, int]:
        """How much is known at the *most specific* rungs, scoped first.

        Deliberately not `Calibration.sample_count`: an uncalibrated record
        reports the widest count it has, so that a screen can say "checked
        three times, which is not enough" instead of falling silent. Ranking
        on that number would call a transit key well-covered because the same
        provider's *driving* record is full - which is the one distinction
        this whole exercise exists to keep.
        """
        return (
            len(
                _matching(
                    corpus,
                    provider=p.provider,
                    dimension=p.dimension,
                    mode=p.mode,
                    scope=p.scope,
                )
            ),
            len(_matching(corpus, provider=p.provider, dimension=p.dimension, mode=p.mode)),
        )

    # Least-known first, then a stable tiebreak so the same trip asks the same
    # questions on every read.
    candidates.sort(key=lambda p: (*known(p), p.subject))
    return candidates[:limit]


def question_text(prediction: Prediction) -> str:
    template = DIMENSIONS[prediction.dimension].question
    if template is None:
        return prediction.subject_label
    return template.format(what=prediction.subject_label, value=f"{prediction.value:g}")


def evidence_line(outcome: Outcome) -> str:
    """One check, in the words it was given in.

    A check that matched says so rather than printing the same number twice,
    and the dimension is named in its own label - "256 currency" is a unit
    key leaking onto a screen.
    """
    entry = DIMENSIONS[outcome.dimension]
    said = _format(outcome.predicted, entry.unit)
    how = outcome.checked_by.replace("_", " ")

    if outcome.actual_low == outcome.actual_high:
        if outcome.actual_low == outcome.predicted:
            return f"{entry.label}: said {said} — matched ({how})"
        return (
            f"{entry.label}: said {said} — was "
            f"{_format(outcome.actual_low, entry.unit)} ({how})"
        )
    return f"{entry.label}: said {said} — {outcome.answer or 'checked'} ({how})"


def dimensions_never_checked(outcomes: Iterable[Outcome]) -> list[Dimension]:
    """The ones with no corpus at all, so a surface can say so out loud.

    Rendering nothing for these would let "we have never once checked whether
    this is right" and "this is right" look identical on screen.
    """
    seen = {o.dimension for o in outcomes}
    return [d for d in DIMENSIONS if d not in seen]
