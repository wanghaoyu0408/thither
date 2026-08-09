"""Finding the disagreements a group score would otherwise bury.

This is the milestone's acceptance in one file: *identify major preference
conflicts instead of hiding them behind an average score.* A `GroupScore` keeps
the split visible, but a number cannot say what people are actually arguing
about. That is what these records are for.

Pure and derived, in the mould of `signals_from_evidence`: computed from stored
preferences at read time rather than kept alongside them, so it is impossible
for the conflicts on screen to be out of step with the preferences behind them.
What *is* stored is the `OpenQuestion` a blocking conflict raises, because an
answer has to survive the turn that gave it.

Two rules govern severity, and both are about honesty rather than caution:

**"Major" is defined by meaning, not by a cutoff someone liked the look of.**
Two travellers half a scale apart on the same weight really are pulling in
opposite directions. Two 0.1 apart are the same person twice.

**An unknown is never a violation.** Google attests that a place serves
vegetarian food; it never attests that one does not. So a dietary conflict
raises a question and leaves the restaurant on the list, rather than deleting
most of a city on the strength of a field that only ever means "not attested".
"""

from datetime import time

from app.models.group import PreferenceConflict, TravelerPreferences
from app.models.trip import TripState

# The same importance weight this far apart across travellers is a real
# disagreement about what the trip is for, not measurement noise.
IMPORTANCE_GAP = 0.5

# Walking tolerance differing by this factor means one person's comfortable day
# is another's forced march.
WALKING_RATIO = 2.0

# Start times this far apart cannot both be honoured by one itinerary.
START_GAP_MINUTES = 120

_INTENSITY_RANK = {"relaxed": 0, "balanced": 1, "packed": 2}

# Importance weights worth comparing across travellers. The phrase is a bare
# noun so the templates can frame it - "how much {phrase} matters" reads badly
# if the phrase already says "how much".
_IMPORTANCE_FIELDS: list[tuple[str, str, str]] = [
    ("flight", "price_importance", "flight price"),
    ("flight", "nonstop_importance", "flying nonstop"),
    ("hotel", "price_importance", "hotel price"),
    ("hotel", "location_importance", "hotel location"),
    ("hotel", "quiet_importance", "a quiet room"),
    ("food", "fine_dining_interest", "fine dining"),
    ("food", "queue_tolerance", "queueing for food"),
    ("activity", "museum_interest", "museums"),
    ("activity", "nightlife_interest", "nightlife"),
    ("activity", "shopping_interest", "shopping"),
]

# Itinerary item types where a dietary restriction actually bites.
_MEAL_TYPES = ("restaurant", "cafe", "meal", "food")


def detect_conflicts(state: TripState) -> list[PreferenceConflict]:
    """Every disagreement worth telling the group about, most serious first."""
    travelers = {
        traveler.traveler_id: traveler.preferences
        for traveler in state.travelers
        if traveler.preferences is not None
    }
    names = {traveler.traveler_id: traveler.name for traveler in state.travelers}

    if len(travelers) < 1:
        return []

    conflicts: list[PreferenceConflict] = []
    conflicts.extend(_dietary(state, travelers, names))
    conflicts.extend(_accessibility(state, travelers, names))

    if len(travelers) >= 2:
        conflicts.extend(_airline(travelers, names))
        conflicts.extend(_interest(travelers, names))
        conflicts.extend(_importance(travelers, names))
        conflicts.extend(_pace(travelers, names))
        conflicts.extend(_schedule(travelers, names))

    conflicts.extend(_budget(state, travelers, names))

    order = {"blocking": 0, "material": 1, "minor": 2}
    conflicts.sort(key=lambda conflict: (order[conflict.severity], conflict.kind))
    return conflicts


def unresolved_blocking(state: TripState) -> list[PreferenceConflict]:
    """Blocking conflicts nobody has answered yet.

    An answered `OpenQuestion` naming a conflict is the group having decided.
    The check is on the answer rather than on the preferences changing, because
    "we know, we are eating there anyway" is a legitimate resolution that leaves
    everyone's preferences exactly as they were.

    Questions raised for a conflict carry its id in their text, which is why
    `PreferenceConflict.conflict_id` is a hash of the disagreement rather than a
    fresh value each time - a derived record needs a derived identity, or the
    answer could never be matched to the next derivation of the same question.
    """
    settled = " ".join(question.question for question in state.open_questions if question.answered)

    return [
        conflict
        for conflict in detect_conflicts(state)
        if conflict.blocking and conflict.conflict_id not in settled
    ]


# --- dietary and accessibility ------------------------------------------------


def _scheduled_meals(state: TripState) -> list[tuple[str, str, bool | None]]:
    """(item_id, title, verified) for every meal on the itinerary."""
    meals: list[tuple[str, str, bool | None]] = []
    for _day, item in state.itinerary.iter_items():
        if item.type not in _MEAL_TYPES:
            continue
        entity = state.entities.get(item.entity_id or "")
        meals.append((item.item_id, item.title, entity.serves_vegetarian if entity else None))
    return meals


def _dietary(
    state: TripState, travelers: dict[str, TravelerPreferences], names: dict[str, str]
) -> list[PreferenceConflict]:
    """A restriction against meals nothing has confirmed are suitable.

    Deliberately raises a question rather than filtering. Google says a place
    *does* serve vegetarian food; its silence is not a denial, so removing every
    unattested restaurant would delete most of Tokyo from a vegetarian's trip on
    no evidence at all.
    """
    restricted = {
        traveler_id: preferences.food.dietary_restrictions
        for traveler_id, preferences in travelers.items()
        if preferences.food.dietary_restrictions
    }
    if not restricted:
        return []

    meals = _scheduled_meals(state)
    if not meals:
        return []

    unverified = [(item_id, title) for item_id, title, verified in meals if verified is not True]
    if not unverified:
        return []

    who = ", ".join(names.get(tid, tid) for tid in sorted(restricted))
    all_restrictions = sorted({r for values in restricted.values() for r in values})

    return [
        PreferenceConflict(
            kind="dietary",
            severity="blocking",
            traveler_ids=sorted(restricted),
            summary=(
                f"{who} follows a {', '.join(all_restrictions)} diet, and "
                f"{len(unverified)} of {len(meals)} planned meal(s) are not confirmed to suit "
                f"it. Google only ever confirms that a place does serve suitable food - it "
                f"never confirms the opposite - so these are unverified, not unsuitable."
            ),
            positions={
                tid: f"needs {', '.join(values)}" for tid, values in sorted(restricted.items())
            },
            affects=[item_id for item_id, _title in unverified],
            resolution_options=[
                f"check the menu for {', '.join(title for _id, title in unverified[:3])}",
                "swap in restaurants Google confirms serve suitable food",
                "accept them as they are and note that the group checked",
            ],
        )
    ]


def _accessibility(
    state: TripState, travelers: dict[str, TravelerPreferences], names: dict[str, str]
) -> list[PreferenceConflict]:
    """A stated mobility limit against venues nothing has confirmed are reachable."""
    limited = {
        traveler_id: preferences.pace.max_daily_walking_km
        for traveler_id, preferences in travelers.items()
        if preferences.pace.max_daily_walking_km <= 3.0
    }
    if not limited:
        return []

    unverified: list[str] = []
    total = 0
    for _day, item in state.itinerary.iter_items():
        if not item.entity_id:
            continue
        total += 1
        entity = state.entities.get(item.entity_id)
        if entity is None or not entity.accessibility.get("wheelchairAccessibleEntrance"):
            unverified.append(item.item_id)

    if not unverified or not total:
        return []

    who = ", ".join(names.get(tid, tid) for tid in sorted(limited))
    return [
        PreferenceConflict(
            kind="accessibility",
            severity="blocking",
            traveler_ids=sorted(limited),
            summary=(
                f"{who} can walk at most "
                f"{min(limited.values()):g} km a day, and {len(unverified)} of {total} "
                f"stop(s) have no confirmed step-free entrance. Google attests accessibility "
                f"when it knows it; silence here means unknown, not inaccessible."
            ),
            positions={
                tid: f"at most {km:g} km walking per day" for tid, km in sorted(limited.items())
            },
            affects=unverified,
            resolution_options=[
                "check step-free access for these venues",
                "prefer venues with confirmed accessible entrances",
                "plan taxis between stops and note it on the day",
            ],
        )
    ]


# --- traveller against traveller ---------------------------------------------


def _airline(
    travelers: dict[str, TravelerPreferences], names: dict[str, str]
) -> list[PreferenceConflict]:
    conflicts: list[PreferenceConflict] = []

    preferred: dict[str, set[str]] = {
        tid: {code.upper() for code in prefs.flight.preferred_airlines}
        for tid, prefs in travelers.items()
    }
    avoided: dict[str, set[str]] = {
        tid: {code.upper() for code in prefs.flight.avoided_airlines}
        for tid, prefs in travelers.items()
    }

    for wanted_by, wanted in preferred.items():
        for avoided_by, unwanted in avoided.items():
            if wanted_by == avoided_by:
                continue
            clash = wanted & unwanted
            if not clash:
                continue
            airlines = ", ".join(sorted(clash))
            conflicts.append(
                PreferenceConflict(
                    kind="airline",
                    severity="material",
                    traveler_ids=sorted({wanted_by, avoided_by}),
                    summary=(
                        f"{names.get(wanted_by, wanted_by)} prefers {airlines} and "
                        f"{names.get(avoided_by, avoided_by)} asked to avoid it. An avoided "
                        f"airline is filtered for the whole group, so this removes flights "
                        f"one of them wanted."
                    ),
                    positions={
                        wanted_by: f"prefers {airlines}",
                        avoided_by: f"asked to avoid {airlines}",
                    },
                    affects=["flights"],
                    resolution_options=[
                        f"drop the objection to {airlines} for this trip",
                        f"drop the preference for {airlines} and compare on price and timing",
                        "book separately",
                    ],
                )
            )
    return conflicts


def _interest(
    travelers: dict[str, TravelerPreferences], names: dict[str, str]
) -> list[PreferenceConflict]:
    conflicts: list[PreferenceConflict] = []

    for wants_id, wants in travelers.items():
        for avoids_id, avoids in travelers.items():
            if wants_id >= avoids_id:
                continue
            clash = {i.lower() for i in wants.activity.interests} & {
                a.lower() for a in avoids.activity.avoided
            }
            reverse = {i.lower() for i in avoids.activity.interests} & {
                a.lower() for a in wants.activity.avoided
            }
            pairs = ((clash, wants_id, avoids_id), (reverse, avoids_id, wants_id))
            for topics, keen, unkeen in pairs:
                if not topics:
                    continue
                listed = ", ".join(sorted(topics))
                conflicts.append(
                    PreferenceConflict(
                        kind="interest",
                        severity="material",
                        traveler_ids=sorted({keen, unkeen}),
                        summary=(
                            f"{names.get(keen, keen)} came for {listed}; "
                            f"{names.get(unkeen, unkeen)} asked to avoid it."
                        ),
                        positions={
                            keen: f"interested in {listed}",
                            unkeen: f"asked to avoid {listed}",
                        },
                        affects=["itinerary"],
                        resolution_options=[
                            f"give {listed} its own slot for whoever wants it",
                            "split the group for part of a day",
                            "drop it from the shared plan",
                        ],
                    )
                )
    return conflicts


def _importance(
    travelers: dict[str, TravelerPreferences], names: dict[str, str]
) -> list[PreferenceConflict]:
    """The same weight at opposite ends of the scale.

    This is the conflict a mean is worst at: two travellers at 0.9 and 0.1
    average to exactly the same 0.5 as two who genuinely do not care much.
    """
    conflicts: list[PreferenceConflict] = []

    for section, field, phrase in _IMPORTANCE_FIELDS:
        values = {
            tid: getattr(prefs.section(section), field)
            for tid, prefs in travelers.items()
            if prefs.section(section) is not None
        }
        if len(values) < 2:
            continue

        high = max(values, key=lambda tid: (values[tid], tid))
        low = min(values, key=lambda tid: (values[tid], tid))
        gap = values[high] - values[low]
        if gap < IMPORTANCE_GAP:
            continue

        conflicts.append(
            PreferenceConflict(
                kind="importance",
                severity="material",
                traveler_ids=sorted({high, low}),
                summary=(
                    f"{names.get(high, high)} and {names.get(low, low)} disagree on how much "
                    f"{phrase} matters: {values[high]:.1f} against {values[low]:.1f}. "
                    f"Averaging them would describe a preference neither of them has."
                ),
                positions={
                    high: f"{phrase} matters {values[high]:.1f}",
                    low: f"{phrase} matters {values[low]:.1f}",
                },
                affects=list(_AFFECTED_BY_SECTION.get(section, ("itinerary",))),
                resolution_options=[
                    f"decide as a group how much {phrase} counts",
                    "let whoever cares more choose this one and trade it against another",
                ],
            )
        )
    return conflicts


_AFFECTED_BY_SECTION: dict[str, tuple[str, ...]] = {
    "flight": ("flights",),
    "hotel": ("hotel", "hotel_area"),
    "food": ("itinerary",),
    "activity": ("itinerary",),
    "pace": ("itinerary",),
}


def _pace(
    travelers: dict[str, TravelerPreferences], names: dict[str, str]
) -> list[PreferenceConflict]:
    conflicts: list[PreferenceConflict] = []

    intensities = {tid: prefs.pace.intensity for tid, prefs in travelers.items()}
    ranks = {tid: _INTENSITY_RANK[value] for tid, value in intensities.items()}
    if ranks and max(ranks.values()) - min(ranks.values()) >= 2:
        fastest = max(ranks, key=lambda tid: (ranks[tid], tid))
        slowest = min(ranks, key=lambda tid: (ranks[tid], tid))
        conflicts.append(
            PreferenceConflict(
                kind="pace",
                severity="material",
                traveler_ids=sorted({fastest, slowest}),
                summary=(
                    f"{names.get(fastest, fastest)} wants a {intensities[fastest]} trip and "
                    f"{names.get(slowest, slowest)} wants a {intensities[slowest]} one. One "
                    f"itinerary cannot be both."
                ),
                positions={
                    fastest: f"{intensities[fastest]} pace",
                    slowest: f"{intensities[slowest]} pace",
                },
                affects=["itinerary"],
                resolution_options=[
                    "plan a balanced pace with optional extras for whoever wants more",
                    "alternate busy and quiet days",
                ],
            )
        )

    walking = {tid: prefs.pace.max_daily_walking_km for tid, prefs in travelers.items()}
    if len(walking) >= 2:
        most = max(walking, key=lambda tid: (walking[tid], tid))
        least = min(walking, key=lambda tid: (walking[tid], tid))
        if walking[least] > 0 and walking[most] / walking[least] >= WALKING_RATIO:
            conflicts.append(
                PreferenceConflict(
                    kind="pace",
                    severity="material",
                    traveler_ids=sorted({most, least}),
                    summary=(
                        f"{names.get(most, most)} is happy walking {walking[most]:g} km a day; "
                        f"{names.get(least, least)} tops out at {walking[least]:g} km. Days "
                        f"built for one will not suit the other."
                    ),
                    positions={
                        most: f"up to {walking[most]:g} km a day",
                        least: f"up to {walking[least]:g} km a day",
                    },
                    affects=["itinerary"],
                    resolution_options=[
                        f"build days around {walking[least]:g} km and add optional walks",
                        "plan transit between stops rather than walking them",
                    ],
                )
            )
    return conflicts


def _minutes(clock: str) -> int:
    parsed = time.fromisoformat(clock)
    return parsed.hour * 60 + parsed.minute


def _schedule(
    travelers: dict[str, TravelerPreferences], names: dict[str, str]
) -> list[PreferenceConflict]:
    starts = {tid: _minutes(prefs.pace.preferred_start_time) for tid, prefs in travelers.items()}
    if len(starts) < 2:
        return []

    latest = max(starts, key=lambda tid: (starts[tid], tid))
    earliest = min(starts, key=lambda tid: (starts[tid], tid))
    if starts[latest] - starts[earliest] < START_GAP_MINUTES:
        return []

    return [
        PreferenceConflict(
            kind="schedule",
            severity="minor",
            traveler_ids=sorted({latest, earliest}),
            summary=(
                f"{names.get(earliest, earliest)} likes starting at "
                f"{travelers[earliest].pace.preferred_start_time} and "
                f"{names.get(latest, latest)} at "
                f"{travelers[latest].pace.preferred_start_time}."
            ),
            positions={
                earliest: f"starts at {travelers[earliest].pace.preferred_start_time}",
                latest: f"starts at {travelers[latest].pace.preferred_start_time}",
            },
            affects=["itinerary"],
            resolution_options=[
                "start late and let early risers do something first",
                "put the fixed bookings mid-morning",
            ],
        )
    ]


def _budget(
    state: TripState, travelers: dict[str, TravelerPreferences], names: dict[str, str]
) -> list[PreferenceConflict]:
    """A traveller's stated hotel ceiling against what the trip plans to spend."""
    ceiling = state.brief.budget.hotel_per_night
    if ceiling is None:
        return []

    decision = state.decisions.hotel
    if decision is None or not decision.selected_option_id:
        return []

    chosen = next(
        (
            option.data
            for option in decision.options
            if option.option_id == decision.selected_option_id
        ),
        None,
    )
    price = getattr(chosen, "nightly_price", None)
    if price is None:
        return []

    over = {
        tid: prefs.hotel.price_importance
        for tid, prefs in travelers.items()
        if prefs.hotel.price_importance >= 0.7
    }
    if not over or price.amount <= ceiling:
        return []

    who = ", ".join(names.get(tid, tid) for tid in sorted(over))
    return [
        PreferenceConflict(
            kind="budget",
            severity="material",
            traveler_ids=sorted(over),
            summary=(
                f"The selected hotel is {price.amount:.0f} {price.currency} a night against a "
                f"{ceiling:.0f} budget, and {who} said price matters a lot."
            ),
            positions={
                tid: f"price importance {weight:.1f}" for tid, weight in sorted(over.items())
            },
            affects=["hotel"],
            resolution_options=[
                "raise the nightly budget with everyone's agreement",
                "look again in the same area under the budget",
            ],
        )
    ]
