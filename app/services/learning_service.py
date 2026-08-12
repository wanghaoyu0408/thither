"""Signals in, hypotheses out. Nothing here writes a profile.

The division of labour, stated once:

  * **Signals are stored facts** - what happened, attributed to exactly one
    profile, written by the action endpoints, the reflection endpoint and the
    `record_stated_preference` tool.
  * **Hypotheses are derived, never stored** - recomputed from the signals on
    every read (the `signals_from_evidence` / `conflict_service` mould), with
    content-hash identities so an answer given today matches the same
    hypothesis derived tomorrow.
  * **The only path from a hypothesis into a profile is the consent
    endpoint** - `POST /profiles/{id}/learning/{hypothesis_id}/accept` -
    which the traveller drives. This module only prepares the change.

Strength is how intensely a preference was ever expressed (max over
signals); confidence is how much evidence there is (count and trip breadth).
They are never folded into one another, and both are ordinal words - a
percentage computed from three clicks would be fake precision.
"""

from dataclasses import dataclass
from datetime import time
from typing import Any, Callable

from app.config import Settings
from app.models.common import Money
from app.models.decision import Decision, FlightOptionData, HotelOptionData
from app.models.learning import (
    Confidence,
    HypothesisEvidence,
    LearnedProvenance,
    LearningSignal,
    PreferenceHypothesis,
    PreferenceKey,
    SignalStrength,
    TripReflection,
)
from app.models.traveler import TravelerProfile
from app.models.trip import TripState

# Definitional cutoffs live beside the code that means them, not in Settings
# (the conflict_service convention): what counts as "early" or "a real move"
# defines the signal, it does not tune eagerness.
EARLY_START = time(10, 0)  # an item before this is an early morning
MOVE_LATER_MINUTES = 60  # a smaller nudge is scheduling, not preference

# "strong" confidence needs twice the proposable evidence and one more
# distinct trip - a pattern is strong when it has survived repetition, not
# when someone clicked emphatically.
STRONG_SIGNAL_FACTOR = 2
STRONG_TRIP_BONUS = 1

# Proposed-value rules for avoid_early_mornings. The proposal is the latest
# start the traveller demonstrably chose, rounded down to the half hour and
# bounded - learning may say "later mornings", it may not say "start at noon"
# on the strength of one dramatic drag.
DEFAULT_PROPOSED_START = "10:30"
EARLIEST_PROPOSED_START = "09:30"
LATEST_PROPOSED_START = "11:30"

QUEUE_TOLERANCE_PROPOSED = 0.2

# What a run of choices proposes for an importance weight (default 0.5).
# 0.7 is not a new invention: `conflict_service` already treats >= 0.7 as
# "this person cares about price" when it decides whether a budget gap is
# worth raising. Learning raises a weight to the codebase's own existing
# threshold for caring - it never proposes 1.0, because a handful of clicks
# is not somebody saying a thing is all that matters.
IMPORTANCE_PROPOSED = 0.7

# Two prices are the same price under this. Not a materiality threshold -
# a choice between equal prices simply gave nothing up, so there is no
# preference in it to read.
SAME_PRICE_EPSILON = 1.0

_STRENGTH_ORDER: dict[SignalStrength, int] = {"weak": 0, "moderate": 1, "strong": 2}


def _round_down_half_hour(clock: str) -> str:
    hh, mm = clock.split(":")
    return f"{hh}:{'30' if int(mm) >= 30 else '00'}"


def _proposed_start(signals: list[LearningSignal]) -> str:
    """The latest start the evidence itself names, rounded and clamped."""
    observed = [
        s.context["to"]
        for s in signals
        if isinstance(s.context.get("to"), str) and len(s.context["to"]) == 5
    ]
    if not observed:
        return DEFAULT_PROPOSED_START
    # HH:MM strings compare lexicographically as times.
    candidate = _round_down_half_hour(max(observed))
    return min(max(candidate, EARLIEST_PROPOSED_START), LATEST_PROPOSED_START)


@dataclass(frozen=True)
class CatalogueEntry:
    """One learnable preference, and - non-negotiably - who consumes it.

    A key with no consumer is the recurring defect this project keeps
    finding (README ledger 10/11: a stated preference that influences
    nothing), so the consumer is part of the catalogue record.
    """

    field_path: str  # dotted: "<profile section>.<field>"
    label: str  # the phrase cards use
    consumer: str  # prose: what actually reads the accepted value
    proposed_value: Callable[[list[LearningSignal]], Any]
    already_satisfied: Callable[[TravelerProfile, Any], bool]


CATALOGUE: dict[PreferenceKey, CatalogueEntry] = {
    "avoid_early_mornings": CatalogueEntry(
        field_path="pace_preferences.preferred_start_time",
        label="Prefers later mornings",
        consumer="itinerary_service shifts every day's slots to the group's latest preferred start",
        proposed_value=_proposed_start,
        already_satisfied=lambda p, v: p.pace_preferences.preferred_start_time >= v,
    ),
    "relaxed_pace": CatalogueEntry(
        field_path="pace_preferences.intensity",
        label="Relaxed pace",
        consumer="conflict detection and the day templates, via the resolved snapshot",
        proposed_value=lambda _s: "relaxed",
        already_satisfied=lambda p, v: p.pace_preferences.intensity == v,
    ),
    "packed_pace": CatalogueEntry(
        field_path="pace_preferences.intensity",
        label="Packed days",
        consumer="conflict detection and the day templates, via the resolved snapshot",
        proposed_value=lambda _s: "packed",
        already_satisfied=lambda p, v: p.pace_preferences.intensity == v,
    ),
    "parking_sensitive": CatalogueEntry(
        field_path="pace_preferences.parking_sensitive",
        label="Parking matters",
        consumer="arrival_penalty doubles parking friction in substitute ranking and its recorded score",
        proposed_value=lambda _s: True,
        already_satisfied=lambda p, v: p.pace_preferences.parking_sensitive is True,
    ),
    "dislikes_queueing": CatalogueEntry(
        field_path="food_preferences.queue_tolerance",
        label="Hates queueing",
        consumer="the queue-tolerance conflict row in conflict detection",
        proposed_value=lambda _s: QUEUE_TOLERANCE_PROPOSED,
        already_satisfied=lambda p, v: p.food_preferences.queue_tolerance <= v,
    ),
    "values_nonstop": CatalogueEntry(
        field_path="flight_preferences.nonstop_importance",
        label="Pays for nonstop",
        consumer="flight_ranking weights its nonstop dimension by exactly this number",
        proposed_value=lambda _s: IMPORTANCE_PROPOSED,
        already_satisfied=lambda p, v: p.flight_preferences.nonstop_importance >= v,
    ),
    "flight_price_sensitive": CatalogueEntry(
        field_path="flight_preferences.price_importance",
        label="Flies on price",
        consumer="flight_ranking weights its price dimension by exactly this number",
        proposed_value=lambda _s: IMPORTANCE_PROPOSED,
        already_satisfied=lambda p, v: p.flight_preferences.price_importance >= v,
    ),
    "hotel_location_matters": CatalogueEntry(
        field_path="hotel_preferences.location_importance",
        label="Pays to stay close",
        consumer="hotel_ranking weights its location dimension by exactly this number",
        proposed_value=lambda _s: IMPORTANCE_PROPOSED,
        already_satisfied=lambda p, v: p.hotel_preferences.location_importance >= v,
    ),
    "hotel_price_sensitive": CatalogueEntry(
        field_path="hotel_preferences.price_importance",
        label="Books on price",
        consumer="hotel_ranking's price dimension, and the budget-gap conflict row at >= 0.7",
        proposed_value=lambda _s: IMPORTANCE_PROPOSED,
        already_satisfied=lambda p, v: p.hotel_preferences.price_importance >= v,
    ),
}


# --- attribution -------------------------------------------------------------


def behavioral_signal_allowed(state: TripState) -> str | None:
    """The profile a UI action may be attributed to, or None.

    Only a trip with exactly one traveller who has a stored profile gives an
    unambiguous answer to "who did that?". Anything else returns None and the
    behaviour goes unrecorded - learning less beats learning about the wrong
    person (acceptance 9).
    """
    if len(state.travelers) != 1:
        return None
    return state.travelers[0].profile_id


# --- signal builders ---------------------------------------------------------


def signal_for_move(
    state: TripState,
    *,
    item_title: str,
    old_start: time,
    new_start: time,
    profile_id: str,
) -> LearningSignal | None:
    """A later-start signal, when the move actually says that.

    Clock time only - moving an 08:30 activity to 08:30 on another day says
    nothing about mornings. Small nudges are scheduling, not preference.
    """
    if old_start >= EARLY_START:
        return None
    delta = (new_start.hour * 60 + new_start.minute) - (old_start.hour * 60 + old_start.minute)
    if delta < MOVE_LATER_MINUTES:
        return None
    return LearningSignal(
        profile_id=profile_id,
        trip_id=state.trip_id,
        preference_key="avoid_early_mornings",
        strength="weak",
        source="behavior_move",
        context={
            "item": item_title,
            "from": old_start.strftime("%H:%M"),
            "to": new_start.strftime("%H:%M"),
            "trip_title": state.metadata.title,
        },
    )


def signal_for_replan(
    state: TripState, *, intensity: str | None, when: str, profile_id: str
) -> LearningSignal | None:
    """An explicit easier/harder replan is a pace statement; a plain one is not."""
    key: PreferenceKey | None = {"relaxed": "relaxed_pace", "packed": "packed_pace"}.get(
        intensity or ""
    )
    if key is None:
        return None
    return LearningSignal(
        profile_id=profile_id,
        trip_id=state.trip_id,
        preference_key=key,
        strength="weak",
        source="behavior_replan",
        context={
            "day": when,
            "asked_for": intensity,
            "trip_title": state.metadata.title,
        },
    )


def _money(value: Money | None) -> str:
    if value is None:
        return "?"
    symbol = "$" if value.currency == "USD" else f"{value.currency} "
    return f"{symbol}{value.amount:,.0f}"


def _total_stops(data: FlightOptionData) -> int | None:
    """Stops in both directions. A nonstop out with a two-stop return is not
    a nonstop trip, and the scalar field describes the outbound only.

    `FlightSlice.stops` counts segments, so a slice carrying none reports
    zero - which reads as "nonstop" and is really "nobody said". Fall back to
    whatever the provider stated rather than learn a nonstop that isn't one.
    """
    if any(s.segments for s in data.slices):
        return sum(s.stops for s in data.slices)
    return data.stops


def _stops_label(stops: int | None, legs: int) -> str:
    """The stop count in the vocabulary the cards already use.

    A return trip's total has to say it is a total. The card reports each
    direction on its own line - "Outbound 1 stop", "Return 1 stop" - so a
    bare "2 stops" beside it reads as a third, different figure rather than
    the sum of those two.
    """
    if stops is None:
        return "stops unknown"
    both = legs > 1
    if stops == 0:
        return "nonstop both ways" if both else "nonstop"
    plural = "stops" if stops > 1 else "stop"
    return f"{stops} {plural} in all" if both else f"{stops} {plural}"


def _flight_label(data: FlightOptionData) -> str:
    return f"{_money(data.price)}, {_stops_label(_total_stops(data), len(data.slices) or 1)}"


def _hotel_price(data: HotelOptionData) -> Money | None:
    return data.nightly_price or data.total_price


def _hotel_price_amount(data: HotelOptionData) -> float | None:
    price = _hotel_price(data)
    return price.amount if price is not None else None


def _hotel_label(data: HotelOptionData) -> str:
    minutes = data.mean_route_minutes()
    where = f"{minutes:.0f} min from your places" if minutes is not None else "distance unknown"
    return f"{data.name} at {_money(_hotel_price(data))}, {where}"


def _what_the_choice_cost[T](
    chosen: T,
    alternatives: list[T],
    price_of: Callable[[T], float | None],
    cost_of: Callable[[T], float | None],
) -> tuple[str, T] | None:
    """Which way a tradeoff went, and against which option it was made.

    `cost_of` is anything the traveller would rather have less of - stops,
    minutes from where they are going. Returns:

      * `("quality", other)` - they paid more than `other` and got a lower
        cost for it.
      * `("price", other)` - they took the cheaper option while `other`, on
        the same card, had the lower cost.
      * `None` - the choice gave nothing up. A winner that is both cheapest
        and best teaches nothing about priorities, however firmly it was
        clicked, and reading a preference out of it would be inventing one.
    """
    my_price, my_cost = price_of(chosen), cost_of(chosen)
    if my_price is None or my_cost is None:
        return None
    comparable = [
        (a, p, c)
        for a in alternatives
        if (p := price_of(a)) is not None and (c := cost_of(a)) is not None
    ]
    if not comparable:
        return None

    cheaper = [(a, p, c) for a, p, c in comparable if p < my_price - SAME_PRICE_EPSILON]
    if cheaper:
        # They passed over cheaper money. Only a lower cost explains that;
        # if the cheapest was also the better one, this choice was made on
        # something we do not measure, and we say nothing.
        against, _, its_cost = min(cheaper, key=lambda row: row[1])
        return ("quality", against) if my_cost < its_cost else None

    # Nothing on the card was cheaper. If something dearer was better, the
    # money is what they kept.
    dearer_and_better = [
        (a, p, c) for a, p, c in comparable if p > my_price + SAME_PRICE_EPSILON and c < my_cost
    ]
    if dearer_and_better:
        against, _, _ = min(dearer_and_better, key=lambda row: row[2])
        return ("price", against)
    return None


def signals_for_choice(
    state: TripState,
    *,
    decision_name: str,
    decision: Decision,
    chosen_option_id: str,
    profile_id: str,
) -> list[LearningSignal]:
    """What picking one card over the others says, when it says anything.

    Only flights and hotels: they are the decisions whose options carry both
    a price and something to trade it against. Airports and neighbourhoods
    have no price on them at all, so a choice between them is not a tradeoff
    this can read, and inventing an axis for it would be worse than silence.

    Weak, like every other click. Choosing the nonstop once is a Tuesday;
    the thresholds decide when a run of Tuesdays is a preference.
    """
    chosen = next((o for o in decision.options if o.option_id == chosen_option_id), None)
    if chosen is None:
        return []
    # A rejected option is not a road not taken - it is one already refused,
    # and it is not on the card being chosen from.
    others = [
        o for o in decision.options if o.option_id != chosen_option_id and o.status != "rejected"
    ]
    if not others:
        return []

    key: PreferenceKey
    if isinstance(chosen.data, FlightOptionData):
        flights = [o for o in others if isinstance(o.data, FlightOptionData)]
        verdict = _what_the_choice_cost(
            chosen.data,
            [o.data for o in flights],
            lambda d: d.price.amount,
            _total_stops,
        )
        if verdict is None:
            return []
        which, against = verdict
        key = "values_nonstop" if which == "quality" else "flight_price_sensitive"
        chose, over = _flight_label(chosen.data), _flight_label(against)
    elif isinstance(chosen.data, HotelOptionData):
        hotels = [o for o in others if isinstance(o.data, HotelOptionData)]
        verdict = _what_the_choice_cost(
            chosen.data,
            [o.data for o in hotels],
            _hotel_price_amount,
            lambda d: d.mean_route_minutes(),
        )
        if verdict is None:
            return []
        which, against = verdict
        key = "hotel_location_matters" if which == "quality" else "hotel_price_sensitive"
        chose, over = _hotel_label(chosen.data), _hotel_label(against)
    else:
        return []

    return [
        LearningSignal(
            profile_id=profile_id,
            trip_id=state.trip_id,
            preference_key=key,
            strength="weak",
            source="behavior_choice",
            context={
                "decision": decision_name,
                "chose": chose,
                "over": over,
                "trip_title": state.metadata.title,
            },
        )
    ]


def signals_for_reflection(
    state: TripState, reflection: TripReflection, profile_id: str
) -> list[LearningSignal]:
    """At most one signal per key: a reflection is one statement, not many.

    Three busy days in one answer must not impersonate the cross-trip breadth
    that `learning_min_trips` exists to demand. `loved` and `notes` are
    stored on the trip but produce nothing here - free text becomes a signal
    only through the agent tool, with the traveller named.
    """
    signals: list[LearningSignal] = []
    if reflection.days_too_busy:
        signals.append(
            LearningSignal(
                profile_id=profile_id,
                trip_id=state.trip_id,
                preference_key="relaxed_pace",
                strength="moderate",
                source="reflection",
                context={
                    "days_too_busy": [d.isoformat() for d in reflection.days_too_busy],
                    "trip_title": state.metadata.title,
                },
            )
        )

    early_skips: list[str] = []
    for skipped in reflection.skipped:
        for _day, item in state.itinerary.iter_items():
            if item.item_id == skipped.item_id and item.start_at is not None:
                if item.start_at.time() < EARLY_START:
                    early_skips.append(
                        f"{skipped.label} ({item.start_at.strftime('%H:%M')})"
                    )
                break
    if early_skips:
        signals.append(
            LearningSignal(
                profile_id=profile_id,
                trip_id=state.trip_id,
                preference_key="avoid_early_mornings",
                strength="moderate",
                source="reflection",
                context={"skipped_early": early_skips, "trip_title": state.metadata.title},
            )
        )
    return signals


# --- derivation --------------------------------------------------------------


def _evidence_line(signal: LearningSignal) -> str:
    """The sentence "Why?" shows, rendered from the stored context alone."""
    ctx = signal.context
    trip = ctx.get("trip_title") or signal.trip_id
    if signal.source == "behavior_move":
        return (
            f"{trip} — moved '{ctx.get('item', 'an activity')}' "
            f"from {ctx.get('from', '?')} to {ctx.get('to', '?')}"
        )
    if signal.source == "behavior_replan":
        return f"{trip} — asked for day {ctx.get('day', '?')} {ctx.get('asked_for', 'easier')}"
    if signal.source == "behavior_choice":
        return f"{trip} — chose {ctx.get('chose', '?')} over {ctx.get('over', '?')}"
    if signal.source == "stated":
        quote = ctx.get("quote", "")
        return f"{trip} — said: “{quote}”"
    if signal.source == "reflection":
        if "days_too_busy" in ctx:
            return f"{trip} — reflection: {', '.join(ctx['days_too_busy'])} felt too busy"
        return f"{trip} — reflection: skipped {', '.join(ctx.get('skipped_early', []))}"
    return f"{trip} — {signal.preference_key}"


def _confidence(count: int, trips: int, settings: Settings) -> Confidence:
    if (
        count >= settings.learning_min_signals * STRONG_SIGNAL_FACTOR
        and trips >= settings.learning_min_trips + STRONG_TRIP_BONUS
    ):
        return "strong"
    if count >= settings.learning_min_signals and trips >= settings.learning_min_trips:
        return "likely"
    return "emerging"


def _dismissed_ids(profile: TravelerProfile) -> set[str]:
    return {
        record.target_id
        for record in profile.learning_rejections
        if record.target_kind == "hypothesis"
    }


def derive_hypotheses(
    profile: TravelerProfile,
    signals: list[LearningSignal],
    *,
    settings: Settings,
) -> list[PreferenceHypothesis]:
    """Every pattern the stored signals currently support, statuses included.

    Pure: same profile + same signals -> same hypotheses, ids and all. Status
    precedence is dismissed > applied > (proposable | emerging):

      * dismissed - the traveller said no; new evidence never resurrects it.
      * applied - the profile already holds it, either through an acceptance
        (provenance present) or because the traveller set it by hand. Never
        re-proposed; further evidence shows as reinforcement only.
    """
    dismissed = _dismissed_ids(profile)
    grouped: dict[tuple[PreferenceKey, str], list[LearningSignal]] = {}
    for signal in signals:
        if signal.preference_key in CATALOGUE:
            grouped.setdefault((signal.preference_key, signal.direction), []).append(signal)

    hypotheses: list[PreferenceHypothesis] = []
    for (key, direction), group in grouped.items():
        entry = CATALOGUE[key]
        group = sorted(group, key=lambda s: s.observed_at)
        trip_ids = list(dict.fromkeys(s.trip_id for s in group))
        strength = max((s.strength for s in group), key=lambda v: _STRENGTH_ORDER[v])
        confidence = _confidence(len(group), len(trip_ids), settings)
        proposed = entry.proposed_value(group)

        candidate = PreferenceHypothesis(
            profile_id=profile.profile_id,
            preference_key=key,
            direction=direction,
            strength=strength,
            confidence=confidence,
            status="emerging",
            field_path=entry.field_path,
            proposed_value=proposed,
            summary=entry.label,
            signal_ids=[s.signal_id for s in group],
            trip_ids=trip_ids,
            evidence=[
                HypothesisEvidence(
                    signal_id=s.signal_id,
                    trip_id=s.trip_id,
                    source=s.source,
                    strength=s.strength,
                    line=_evidence_line(s),
                    observed_at=s.observed_at,
                )
                for s in group
            ],
        )

        provenance = profile.learned.get(entry.field_path)
        if candidate.hypothesis_id in dismissed:
            status = "dismissed"
        elif (
            provenance is not None and provenance.hypothesis_id == candidate.hypothesis_id
        ) or entry.already_satisfied(profile, proposed):
            status = "applied"
        elif confidence != "emerging":
            status = "proposable"
        else:
            status = "emerging"

        hypotheses.append(candidate.model_copy(update={"status": status}))

    hypotheses.sort(
        key=lambda h: (
            {"proposable": 0, "emerging": 1, "applied": 2, "dismissed": 3}[h.status],
            h.preference_key,
        )
    )
    return hypotheses


# --- applying and removing (prepared here, written only by the consent API) --


_SECTION_FOR_PATH = {
    "pace_preferences": "pace_preferences",
    "food_preferences": "food_preferences",
    "flight_preferences": "flight_preferences",
    "hotel_preferences": "hotel_preferences",
}


def profile_changes_for(
    profile: TravelerProfile,
    hypothesis: PreferenceHypothesis,
    value: Any,
    *,
    value_source: str = "proposed",
) -> tuple[dict[str, Any], LearnedProvenance]:
    """The repository-ready change set for one acceptance.

    The deep-merge trap is defused here, in exactly one place:
    `ProfileRepository.update` shallow-merges top-level keys, so a nested
    dict would REPLACE the whole sub-model and reset its siblings. We
    therefore hand back the complete sub-model, rebuilt with `model_copy`,
    which also re-runs pydantic validation on the new value (a malformed
    ClockTime dies here, not in the database).
    """
    section, field = hypothesis.field_path.split(".", 1)
    if section not in _SECTION_FOR_PATH:
        raise ValueError(f"unknown profile section: {section}")

    sub = getattr(profile, section)
    previous = getattr(sub, field)
    updated = type(sub).model_validate({**sub.model_dump(), field: value})

    provenance = LearnedProvenance(
        hypothesis_id=hypothesis.hypothesis_id,
        preference_key=hypothesis.preference_key,
        signal_ids=hypothesis.signal_ids,
        trip_ids=hypothesis.trip_ids,
        previous_value=previous,
        value_source="edited" if value_source == "edited" else "proposed",
        summary=hypothesis.summary,
    )

    learned = {path: record.model_dump(mode="json") for path, record in profile.learned.items()}
    learned[hypothesis.field_path] = provenance.model_dump(mode="json")

    changes: dict[str, Any] = {
        section: updated.model_dump(mode="json"),
        "learned": learned,
    }
    return changes, provenance


def remove_changes_for(profile: TravelerProfile, field_path: str) -> dict[str, Any]:
    """Revert a learned field to what it held before learning touched it.

    The pre-learning value, not the model default - a hand-set 09:30 that
    learning raised to 10:30 goes back to 09:30. The caller appends the
    dismissal; without it, the untouched evidence would re-propose the
    removed value on the very next read.
    """
    provenance = profile.learned.get(field_path)
    if provenance is None:
        raise ValueError(f"nothing learned at {field_path}")

    section, field = field_path.split(".", 1)
    sub = getattr(profile, section)
    previous = provenance.previous_value
    if previous is None:
        previous = type(sub).model_fields[field].get_default()
    updated = type(sub).model_validate({**sub.model_dump(), field: previous})

    learned = {
        path: record.model_dump(mode="json")
        for path, record in profile.learned.items()
        if path != field_path
    }
    return {section: updated.model_dump(mode="json"), "learned": learned}
