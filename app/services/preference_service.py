"""Resolving whose preferences apply, and noticing when they change.

Two rules, both of which exist because a trip has to stay explainable after the
world behind it has moved on:

**The trip reads its own snapshot, never a live profile row.** Profiles are
stored separately and can be edited long after a trip is planned. If planning
looked them up each time, editing a profile next year would silently rewrite why
last year's trip was built that way, and `TripState` would stop being the source
of truth it claims to be. So preferences are resolved once, stored on
`TripTraveler`, and stamped with the profile revision they came from.

**A refresh diffs before it applies.** Pulling a changed profile straight into a
planned trip would move rankings under decisions the group already made. So
`diff_profile` reports what changed and what it would touch, and applying is a
separate, confirmed step that marks the affected decisions stale rather than
rebuilding them.

Trip overrides win. Something said about *this* trip beats a general preference,
and a profile change to an overridden field changes nothing at all - which the
diff says explicitly rather than listing it as a change that does nothing.
"""

from datetime import date
from typing import Any

from pydantic import BaseModel

from app.models.common import utcnow
from app.models.group import TravelerPreferences
from app.models.traveler import TravelerProfile
from app.models.trip import TripState, TripTraveler

# Our section name -> the field it comes from on a TravelerProfile.
SECTIONS: dict[str, str] = {
    "flight": "flight_preferences",
    "hotel": "hotel_preferences",
    "food": "food_preferences",
    "activity": "activity_preferences",
    "pace": "pace_preferences",
}

# Which parts of a trip a change to each section can actually invalidate.
# Deterministic, so "what does this affect?" is answered by the mapping rather
# than by a judgement call at refresh time.
AFFECTED_BY_SECTION: dict[str, tuple[str, ...]] = {
    "flight": ("flights",),
    "hotel": ("hotel", "hotel_area"),
    "food": ("place_shortlists", "itinerary:restaurant"),
    "activity": ("place_shortlists", "itinerary:activity"),
    # Pace decides the shape of every day rather than any one decision.
    "pace": ("itinerary",),
}

# Itinerary item types each section bears on.
_ITEM_TYPES: dict[str, tuple[str, ...]] = {
    "food": ("restaurant",),
    "activity": ("activity", "attraction", "museum", "shopping"),
}


def _normalize_overrides(overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Accept nested or dotted override keys, and both naming conventions.

    A patch is naturally written dotted (`flight.price_importance`), a fixture
    naturally nested. Both mean the same thing, and rejecting either would be a
    trap rather than a rule.
    """
    resolved: dict[str, dict[str, Any]] = {}

    for key, value in (overrides or {}).items():
        if "." in key:
            section, _, field = key.partition(".")
            section = section.removesuffix("_preferences")
            if section in SECTIONS:
                resolved.setdefault(section, {})[field] = value
            continue

        section = key.removesuffix("_preferences")
        if section in SECTIONS and isinstance(value, dict):
            resolved.setdefault(section, {}).update(value)

    return resolved


def resolve(traveler: TripTraveler, profile: TravelerProfile | None = None) -> TravelerPreferences:
    """Profile defaults with the trip's overrides laid on top.

    Field-wise rather than wholesale, the same discipline `resolve_place` uses:
    overriding one weight must not silently reset the four beside it.
    """
    overrides = _normalize_overrides(traveler.profile_overrides)

    sections: dict[str, Any] = {}
    overridden: list[str] = []

    for section, profile_field in SECTIONS.items():
        base = getattr(profile, profile_field) if profile else None
        merged = base.model_copy(deep=True) if base else _default_for(section)

        changes = overrides.get(section) or {}
        accepted = {
            field: value for field, value in changes.items() if field in type(merged).model_fields
        }
        if accepted:
            merged = merged.model_copy(update=accepted)
            overridden.extend(f"{section}.{field}" for field in sorted(accepted))

        sections[section] = merged

    return TravelerPreferences(
        **sections,
        source_profile_id=profile.profile_id if profile else None,
        source_profile_revision=profile.revision if profile else None,
        overridden_paths=sorted(overridden),
        resolved_at=utcnow(),
    )


def _default_for(section: str):
    return type(TravelerPreferences().section(section))()


def effective(state: TripState) -> tuple[dict[str, TravelerPreferences], list[str]]:
    """Every traveler's preferences as the trip has them stored.

    A traveler with no snapshot gets neutral defaults *and* a warning. Silently
    substituting the average person is precisely how a group trip ends up
    planned for nobody, so the gap is reported rather than filled.
    """
    resolved: dict[str, TravelerPreferences] = {}
    warnings: list[str] = []

    for traveler in state.travelers:
        if traveler.preferences is not None:
            resolved[traveler.traveler_id] = traveler.preferences
            continue

        resolved[traveler.traveler_id] = TravelerPreferences()
        warnings.append(
            f"{traveler.name} has no resolved preferences, so neutral defaults were used; "
            f"nothing here reflects what they actually want"
        )

    return resolved, warnings


def traveler_names(state: TripState) -> dict[str, str]:
    return {traveler.traveler_id: traveler.name for traveler in state.travelers}


def has_preferences(state: TripState) -> bool:
    return any(traveler.preferences is not None for traveler in state.travelers)


# --- refresh -----------------------------------------------------------------


class FieldChange(BaseModel):
    path: str
    before: Any = None
    after: Any = None


class PreferenceDiff(BaseModel):
    """What a profile has done since the trip took its copy."""

    traveler_id: str
    traveler_name: str
    profile_id: str

    from_revision: int | None = None
    to_revision: int | None = None

    # Only what would actually move: an overridden field is pinned by the trip,
    # so a profile edit to it cannot change the effective value and never shows
    # up here as a change.
    changes: list[FieldChange] = []

    # Paths this trip overrides, and which a refresh therefore leaves alone.
    # Reported because "I edited my profile and nothing happened" is otherwise
    # a mystery, and the honest answer is that this trip says otherwise. No
    # claim is made about whether the profile value behind them moved - the
    # snapshot stores the resolved value, so that is not knowable from here.
    not_refreshed: list[str] = []

    # Decision names and itinerary markers this change bears on, from
    # AFFECTED_BY_SECTION.
    affects: list[str] = []

    @property
    def has_effect(self) -> bool:
        return bool(self.changes)

    def describe(self) -> str:
        if not self.changes:
            base = f"{self.traveler_name}: nothing that would move this trip"
        else:
            base = f"{self.traveler_name}: {len(self.changes)} change(s) that would move this trip"
        if self.not_refreshed:
            base += f" ({len(self.not_refreshed)} field(s) overridden by this trip, left alone)"
        return base


def diff_profile(traveler: TripTraveler, profile: TravelerProfile) -> PreferenceDiff:
    """Compare a stored snapshot against the profile as it stands now.

    Reports; applies nothing. What to do about a changed preference is the
    group's decision, not a consequence of somebody editing their profile.
    """
    snapshot = traveler.preferences
    overridden = set(snapshot.overridden_paths) if snapshot else set()
    fresh = resolve(traveler, profile)

    changes: list[FieldChange] = []
    touched_sections: set[str] = set()

    for section in SECTIONS:
        current = snapshot.section(section) if snapshot else None
        updated = fresh.section(section)

        for field in type(updated).model_fields:
            after = getattr(updated, field)
            before = getattr(current, field) if current is not None else None
            if before == after:
                continue

            changes.append(FieldChange(path=f"{section}.{field}", before=before, after=after))
            touched_sections.add(section)

    affects: list[str] = []
    for section in sorted(touched_sections):
        affects.extend(AFFECTED_BY_SECTION.get(section, ()))

    return PreferenceDiff(
        traveler_id=traveler.traveler_id,
        traveler_name=traveler.name,
        profile_id=profile.profile_id,
        from_revision=snapshot.source_profile_revision if snapshot else None,
        to_revision=profile.revision,
        changes=changes,
        not_refreshed=sorted(overridden),
        affects=list(dict.fromkeys(affects)),
    )


def stale_targets(state: TripState, diffs: list[PreferenceDiff]) -> tuple[list[str], list[date]]:
    """Which stored decisions and days a set of diffs actually touches.

    Narrow on purpose. A changed flight preference has nothing to say about
    which restaurants were shortlisted, and marking the whole trip stale would
    make the signal useless the first time anyone edited anything.
    """
    markers = {marker for diff in diffs if diff.has_effect for marker in diff.affects}
    if not markers:
        return [], []

    decisions: list[str] = []
    for name, _decision in state.decisions.iter_decisions():
        head = name.split(".", 1)[0]
        if head in markers:
            decisions.append(name)

    days: list[date] = []
    if "itinerary" in markers:
        days = [day.date for day in state.itinerary.days]
    else:
        wanted = {
            item_type
            for marker in markers
            if marker.startswith("itinerary:")
            for item_type in _ITEM_TYPES.get(marker.split(":", 1)[1], (marker.split(":", 1)[1],))
        }
        if wanted:
            days = sorted(
                {day.date for day, item in state.itinerary.iter_items() if item.type in wanted}
            )

    return decisions, days
