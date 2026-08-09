"""Referential integrity of a TripState.

These are the invariants that keep ids meaningful: locks address items by id,
the itinerary addresses places by id, and a dangling reference would make
either silently stop working.
"""

from app.models.decision import HotelOptionData, PlaceOption
from app.models.trip import TripState
from app.services.conflict_service import unresolved_blocking
from app.services.geo import haversine_km
from app.services.lock_service import collect_lock_targets

# How far a hotel may sit from the middle of the chosen neighbourhood and still
# be in it. Generous: an area centre is a point standing in for a district.
AREA_RADIUS_KM = 3.0


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return sorted(dupes)


def _check_hotel_area(state: TripState) -> list[str]:
    """A chosen hotel must sit in the chosen neighbourhood (spec section 25).

    The ordering the spec insists on - area first, then hotels - is enforced
    here rather than asked for in a prompt, because a prompt-only rule holds
    only as long as the model remembers it. Skipping the area step stays
    possible through `bypass_area_decision`, which records why on the option;
    what is not possible is skipping it silently.
    """
    decision = state.decisions.hotel
    if decision is None or not decision.selected_option_id:
        return []

    hotel = next(
        (
            option.data
            for option in decision.options
            if option.option_id == decision.selected_option_id
        ),
        None,
    )
    if not isinstance(hotel, HotelOptionData):
        return []

    if hotel.area_bypass_reason:
        # A deliberate, recorded skip. Nothing to check against.
        return []

    area_decision = state.decisions.hotel_area
    area = None
    if area_decision is not None and area_decision.selected_option_id:
        area = next(
            (
                option.data
                for option in area_decision.options
                if option.option_id == area_decision.selected_option_id
            ),
            None,
        )

    if area is None:
        return [
            f"hotel {hotel.name!r} is selected but no hotel_area is: choose a neighbourhood "
            "first, or record a bypass reason on the hotel"
        ]

    if hotel.area_name and hotel.area_name == area.area_name:
        return []

    # Names can differ legitimately - the provider's idea of a district is not
    # ours - so coordinates get the final say when both sides have them.
    if area.center is not None and hotel.lat is not None and hotel.lng is not None:
        distance = haversine_km(hotel.lat, hotel.lng, area.center.lat, area.center.lng)
        if distance <= AREA_RADIUS_KM:
            return []
        return [
            f"hotel {hotel.name!r} is {distance:.1f} km from the selected area "
            f"{area.area_name!r}, beyond the {AREA_RADIUS_KM:g} km that area covers"
        ]

    if hotel.area_name:
        return [
            f"hotel {hotel.name!r} is in {hotel.area_name!r} but the selected area is "
            f"{area.area_name!r}"
        ]

    return []


def _check_ready_without_agreement(state: TripState) -> list[str]:
    """A trip cannot be called ready over an unanswered blocking conflict.

    The only thing a blocking conflict stops. Research, itinerary generation,
    replanning and every search go on exactly as before - what is refused is
    declaring the result finished while somebody's dietary requirement or
    mobility limit is still unverified.

    Enforced here rather than asked for in the prompt, because "ready" is a
    claim the trip makes to whoever reads it next, and a claim the model can
    forget to check is not a guarantee.
    """
    if state.status != "ready":
        return []

    blocking = unresolved_blocking(state)
    if not blocking:
        return []

    return [
        f"trip cannot be marked ready: {len(blocking)} unresolved blocking preference "
        f"conflict(s) - " + "; ".join(conflict.summary.split(".")[0] for conflict in blocking[:2])
    ]


def check_integrity(state: TripState) -> list[str]:
    """Return one human-readable message per broken invariant."""
    problems: list[str] = []

    # Registry keys must agree with the entities they hold.
    for key, entity in state.entities.items():
        if entity.entity_id != key:
            problems.append(f"entities[{key!r}] holds entity_id {entity.entity_id!r}")

    # Evidence must point at places the trip knows, or it backs nothing.
    for evidence_id, record in state.evidence.items():
        if record.evidence_id != evidence_id:
            problems.append(f"evidence[{evidence_id!r}] holds evidence_id {record.evidence_id!r}")
        for entity_id in record.entity_ids:
            if entity_id not in state.entities:
                problems.append(f"evidence {evidence_id!r} references unknown entity {entity_id!r}")

    # Every itinerary reference must resolve to a stored entity.
    item_ids: list[str] = []
    for _day, item in state.itinerary.iter_items():
        item_ids.append(item.item_id)
        if item.entity_id and item.entity_id not in state.entities:
            problems.append(
                f"itinerary item {item.item_id!r} references unknown entity {item.entity_id!r}"
            )

    for duplicate in _duplicates(item_ids):
        problems.append(f"duplicate itinerary item_id {duplicate!r}")

    # A decision's selection must be one of its own options.
    for name, decision in state.decisions.iter_decisions():
        option_ids = [option.option_id for option in decision.options]
        for duplicate in _duplicates(option_ids):
            problems.append(f"decision {name!r} has duplicate option_id {duplicate!r}")
        if decision.selected_option_id and decision.selected_option_id not in option_ids:
            problems.append(
                f"decision {name!r} selects unknown option {decision.selected_option_id!r}"
            )

        # Shortlisted places point at the registry rather than copying facts,
        # so a broken link would leave the option meaningless.
        for option in decision.options:
            if isinstance(option.data, PlaceOption) and option.data.entity_id not in state.entities:
                problems.append(
                    f"decision {name!r} option {option.option_id!r} references unknown "
                    f"entity {option.data.entity_id!r}"
                )

            # A recommendation whose justification has gone missing is worse
            # than one with none: it looks evidenced and is not.
            for evidence_id in option.evidence_refs:
                if evidence_id not in state.evidence:
                    problems.append(
                        f"decision {name!r} option {option.option_id!r} cites unknown "
                        f"evidence {evidence_id!r}"
                    )

    problems.extend(_check_hotel_area(state))
    problems.extend(_check_ready_without_agreement(state))

    # Constraints scoped to a traveler must name a traveler on this trip.
    traveler_ids = [traveler.traveler_id for traveler in state.travelers]
    for duplicate in _duplicates(traveler_ids):
        problems.append(f"duplicate traveler_id {duplicate!r}")
    for constraint in state.constraints:
        if constraint.traveler_id and constraint.traveler_id not in traveler_ids:
            problems.append(
                f"constraint {constraint.id!r} references unknown traveler "
                f"{constraint.traveler_id!r}"
            )

    for duplicate in _duplicates([c.id for c in state.constraints]):
        problems.append(f"duplicate constraint id {duplicate!r}")

    # Locks must point at something that exists, or they protect nothing.
    targets = collect_lock_targets(state.model_dump(mode="json"))
    for lock in state.locks:
        if (lock.target_kind, lock.target_id) not in targets:
            problems.append(
                f"lock {lock.lock_id!r} targets missing {lock.target_kind} {lock.target_id!r}"
            )
    for duplicate in _duplicates([lock.lock_id for lock in state.locks]):
        problems.append(f"duplicate lock_id {duplicate!r}")

    # Itinerary days must be unique and fall inside the trip dates.
    day_dates = [day.date for day in state.itinerary.days]
    for duplicate in _duplicates([d.isoformat() for d in day_dates]):
        problems.append(f"duplicate itinerary day {duplicate}")

    start, end = state.brief.dates.start, state.brief.dates.end
    for day_date in day_dates:
        if start and day_date < start:
            problems.append(f"itinerary day {day_date} is before the trip start {start}")
        if end and day_date > end:
            problems.append(f"itinerary day {day_date} is after the trip end {end}")

    return problems
