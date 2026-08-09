"""Turning provider results into registry entities.

Resolution is keyed on `google_place_id`, which matters twice over: re-running
a discovery script must not duplicate entities, and M4's web research has to
land on the *same* entity when it recognises a restaurant Google already gave
us.
"""

from datetime import datetime, timedelta

from app.models.common import Attestation, new_id, utcnow
from app.models.entity import PlaceEntity
from app.models.place import PlaceSummary

GOOGLE_REF = "google_place_id"


def keep_attested(new: Attestation, old: Attestation) -> Attestation:
    """The tri-state merge rule: unknown never overwrites a confirmed value.

    A confirmed value (either direction) replaces anything - fresher provider
    data wins. But "unknown" is the absence of a claim, and letting it erase a
    confirmed_true from an earlier FULL fetch would lose a fact to a cheaper
    call that simply did not ask.
    """
    return old if new == "unknown" else new


def _merge_attestations(
    new: dict[str, Attestation], old: dict[str, Attestation]
) -> dict[str, Attestation]:
    """Key-wise keep_attested, so one option's silence keeps another's claim."""
    merged = dict(old)
    for key, value in new.items():
        merged[key] = keep_attested(value, merged.get(key, "unknown"))
    return merged


# How old a stored fact may be before it must be re-fetched rather than quoted.
# Matches the 30-day ceiling the Maps terms place on cached coordinates, and
# satisfies spec section 30: no numeric claim from a stale snapshot.
STALE_AFTER = timedelta(days=30)


def index_by_place_id(entities: dict[str, PlaceEntity]) -> dict[str, PlaceEntity]:
    return {
        entity.provider_refs[GOOGLE_REF]: entity
        for entity in entities.values()
        if entity.provider_refs.get(GOOGLE_REF)
    }


def resolve_place(
    summary: PlaceSummary,
    existing: dict[str, PlaceEntity],
    *,
    now: datetime | None = None,
) -> PlaceEntity:
    """Merge a provider result into the registry shape.

    Reuses the existing `entity_id` when this place is already known, so ids
    stay stable and every itinerary reference keeps resolving. Fields absent
    from the summary do not overwrite what is already stored - a BASIC search
    after a FULL details fetch must not wipe the opening hours.
    """
    by_place_id = index_by_place_id(existing)
    known = by_place_id.get(summary.place_id)

    def keep(new_value, old_value):
        return old_value if new_value is None else new_value

    return PlaceEntity(
        entity_id=known.entity_id if known else new_id("ent"),
        provider_refs={**(known.provider_refs if known else {}), GOOGLE_REF: summary.place_id},
        name=keep(summary.name, known.name if known else None) or summary.place_id,
        categories=summary.categories or (known.categories if known else []),
        address=keep(summary.address, known.address if known else None),
        lat=keep(summary.lat, known.lat if known else None) or 0.0,
        lng=keep(summary.lng, known.lng if known else None) or 0.0,
        rating=keep(summary.rating, known.rating if known else None),
        rating_count=keep(summary.rating_count, known.rating_count if known else None),
        price_level=keep(summary.price_level, known.price_level if known else None),
        opening_hours=keep(summary.opening_hours, known.opening_hours if known else None),
        # Only FULL fetches carry attestations, so a later RANKING-tier search
        # arrives all-unknown - and an unknown must never erase a confirmed
        # value. This is `keep()` restated for the tri-state, where the "absent"
        # sentinel is the string "unknown" rather than None.
        serves_vegetarian=keep_attested(
            summary.serves_vegetarian, known.serves_vegetarian if known else "unknown"
        ),
        # A cheap search does not ask for parking, so an absent value there must
        # not wipe what a details fetch already established - the same rule the
        # attestations follow.
        parking_options=(
            summary.parking_options
            if summary.parking_options is not None
            else (known.parking_options if known else None)
        ),
        accessibility=_merge_attestations(
            summary.accessibility, known.accessibility if known else {}
        ),
        timezone=keep(summary.timezone, known.timezone if known else None),
        website_url=keep(summary.website_url, known.website_url if known else None),
        maps_url=keep(summary.maps_url, known.maps_url if known else None),
        facts_updated_at=now or utcnow(),
    )


def resolve_places(
    summaries: list[PlaceSummary],
    existing: dict[str, PlaceEntity],
    *,
    now: datetime | None = None,
) -> list[PlaceEntity]:
    """Resolve a batch, letting later summaries see earlier ones.

    Without the running merge, two summaries for the same place inside one
    batch would mint two entity ids.
    """
    working = dict(existing)
    resolved: list[PlaceEntity] = []
    for summary in summaries:
        entity = resolve_place(summary, working, now=now)
        working[entity.entity_id] = entity
        resolved.append(entity)
    return resolved


def is_stale(entity: PlaceEntity, *, now: datetime | None = None) -> bool:
    reference = now or utcnow()
    updated = entity.facts_updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=reference.tzinfo)
    return (reference - updated) > STALE_AFTER


def stale_entities(
    entities: dict[str, PlaceEntity], *, now: datetime | None = None
) -> list[PlaceEntity]:
    """Entities whose facts must be refreshed before they are quoted."""
    return [entity for entity in entities.values() if is_stale(entity, now=now)]


def place_id_of(entity: PlaceEntity) -> str | None:
    return entity.provider_refs.get(GOOGLE_REF)
