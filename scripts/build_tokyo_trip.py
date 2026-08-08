"""Milestone 2 acceptance: build one Tokyo trip against the real APIs.

    .\\.venv\\Scripts\\python.exe scripts\\build_tokyo_trip.py
    .\\.venv\\Scripts\\python.exe scripts\\build_tokyo_trip.py --trip-id trip_xxx   # re-run

Walks the four acceptance criteria in order:

    search real restaurants -> resolve entities -> calculate routes -> store shortlist

Everything that reaches the trip goes through apply_patch, so locks, rejection
memory and integrity checks all apply exactly as they do for the agent.

Passing --trip-id re-runs against an existing trip; the entity count must not
grow, which is what makes resolution idempotent rather than merely working.
"""

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Base
from app.db.repository import TripNotFound, TripRepository
from app.models.common import new_id
from app.models.patch import PatchOperation, TripPatch
from app.models.place import GetPlaceDetailsInput, PlaceFieldSet, SearchPlacesInput
from app.models.route import GetRoutesInput, LocationRef
from app.models.trip import (
    BudgetSpec,
    DestinationSpec,
    PartySpec,
    TripBrief,
    TripDates,
    TripState,
    TripTraveler,
)
from app.services.entity_service import resolve_places
from app.services.ranking_service import rank_places
from app.services.toolbox import MissingCredentials, Toolbox

# Two neighbourhoods, two purposes. Four searches, twelve detail calls.
AREAS = [
    {"key": "shibuya", "name": "Shibuya", "lat": 35.6595, "lng": 139.7005},
    {"key": "asakusa", "name": "Asakusa", "lat": 35.7148, "lng": 139.7967},
]
PURPOSES = [
    {"key": "dinner", "query": "izakaya", "category": "restaurant", "min_rating": 4.0},
    {"key": "coffee", "query": "specialty coffee", "category": "cafe", "min_rating": 4.2},
]
SHORTLIST_SIZE = 3


def banner(step: str, title: str) -> None:
    print(f"\n{'=' * 72}\n{step}  {title}\n{'-' * 72}")


def new_trip() -> TripState:
    return TripState.new(
        title="Tokyo food trip",
        created_by="user_haoyu",
        brief=TripBrief(
            destination=DestinationSpec(city="Tokyo", country="Japan", flexible=False),
            dates=TripDates(start="2026-10-03", end="2026-10-08"),
            party=PartySpec(adults=4, rooms=2),
            budget=BudgetSpec(total_per_person=2500, hotel_per_night=250),
            priorities=["food", "city exploration"],
            pace="relaxed",
        ),
        travelers=[
            TripTraveler(traveler_id="trv_a", name="Haoyu", role="organizer"),
            TripTraveler(traveler_id="trv_b", name="Alice"),
            TripTraveler(traveler_id="trv_c", name="Bo"),
            TripTraveler(traveler_id="trv_d", name="Dee"),
        ],
    )


async def main(trip_id: str | None) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    try:
        async with Toolbox(settings, sessions) as toolbox, sessions() as session:
            repo = TripRepository(session)

            # --- the trip -------------------------------------------------
            if trip_id:
                try:
                    state = await repo.get(trip_id)
                except TripNotFound:
                    print(f"no such trip: {trip_id}")
                    return 1
                banner("0.", f"Reusing trip {state.trip_id} at revision {state.revision}")
            else:
                state = await repo.create(new_trip())
                banner("0.", f"Created trip {state.trip_id}")
            entities_before = len(state.entities)
            print(
                f"    {state.brief.party.adults} travellers, "
                f"{state.brief.dates.start} to {state.brief.dates.end}, "
                f"priorities {state.brief.priorities}"
            )
            print(f"    entities already stored: {entities_before}")

            # --- 1. search ------------------------------------------------
            banner("1.", "Search real places  (acceptance: search real restaurants)")
            shortlists: dict[str, list] = {}
            candidates_seen = 0

            for area in AREAS:
                for purpose in PURPOSES:
                    result = await toolbox.places.search_places(
                        SearchPlacesInput(
                            query=purpose["query"],
                            categories=[purpose["category"]],
                            lat=area["lat"],
                            lng=area["lng"],
                            radius_meters=1200,
                            min_rating=purpose["min_rating"],
                            limit=20,
                        )
                    )
                    label = f"{area['name']} / {purpose['key']}"

                    if not result.ok:
                        print(
                            f"    {label:24} FAILED  [{result.error.code}] {result.error.message}"
                        )
                        continue
                    if result.found_nothing:
                        print(f"    {label:24} no matches (the API was fine)")
                        continue

                    candidates_seen += len(result.results)

                    ranked = rank_places(
                        result.results,
                        origin=(area["lat"], area["lng"]),
                        min_rating_count=80,
                        limit=SHORTLIST_SIZE,
                    )
                    shortlists[f"{purpose['key']}_{area['key']}"] = ranked

                    print(f"    {label:24} {len(result.results):>2} found -> top {len(ranked)}")
                    for item in ranked:
                        place = item.place
                        print(
                            f"        {item.score.total:.3f}  {place.name}  "
                            f"({place.rating} from {place.rating_count:,} reviews)"
                        )

            if not shortlists:
                print("\nNothing was found or every search failed; stopping.")
                return 1

            # --- 2. details + entities ------------------------------------
            banner("2.", "Fetch details and resolve entities  (acceptance: resolve entities)")
            place_ids = [item.place.place_id for ranked in shortlists.values() for item in ranked]
            details = await toolbox.places.get_place_details(
                GetPlaceDetailsInput(place_ids=place_ids, field_set=PlaceFieldSet.FULL)
            )
            if not details.ok:
                print(f"    details failed: [{details.error.code}] {details.error.message}")
                return 1
            print(
                f"    fetched FULL details for {len(details.results)} shortlisted places, "
                f"not for all {candidates_seen} candidates "
                f"({candidates_seen - len(details.results)} Enterprise calls saved)"
            )

            resolved = resolve_places(details.results, state.entities)
            by_place_id = {entity.provider_refs["google_place_id"]: entity for entity in resolved}
            print(
                f"    resolved into {len(resolved)} entities "
                f"({len({e.entity_id for e in resolved})} distinct ids)"
            )

            entity_ops = [
                PatchOperation(
                    op="add" if entity.entity_id not in state.entities else "set",
                    path=f"/entities/{entity.entity_id}",
                    value=entity.model_dump(mode="json"),
                )
                for entity in resolved
            ]
            result = await repo.apply_patch(
                state.trip_id,
                TripPatch(
                    base_revision=state.revision,
                    reason="store places discovered in Shibuya and Asakusa",
                    actor="system",
                    operations=entity_ops,
                ),
            )
            if not result.applied:
                print(f"    patch rejected: {[e.code for e in result.errors]}")
                for error in result.errors:
                    print(f"        {error.message}  {error.details}")
                return 1
            state = result.state
            print(
                f"    revision {result.revision}, entities now {len(state.entities)} "
                f"(was {entities_before})"
            )

            # --- 3. routes ------------------------------------------------
            banner("3.", "Calculate real travel times  (acceptance: calculate routes)")
            walk_minutes: dict[str, float | None] = {}

            for area in AREAS:
                area_entities = [
                    by_place_id[item.place.place_id]
                    for key, ranked in shortlists.items()
                    if key.endswith(area["key"])
                    for item in ranked
                    if item.place.place_id in by_place_id
                ]
                if not area_entities:
                    continue

                routes = await toolbox.routes.get_routes(
                    GetRoutesInput(
                        origins=[LocationRef(lat=area["lat"], lng=area["lng"], label=area["name"])],
                        destinations=[
                            LocationRef(entity_id=entity.entity_id) for entity in area_entities
                        ],
                        mode="walking",
                    ),
                    entities=state.entities,
                )
                if not routes.ok:
                    print(
                        f"    {area['name']}: routes failed "
                        f"[{routes.error.code}] {routes.error.message}"
                    )
                    continue

                print(f"    walking from {area['name']} station:")
                for leg in routes.results:
                    entity = area_entities[leg.destination_index]
                    minutes = leg.duration_minutes
                    walk_minutes[entity.entity_id] = minutes
                    shown = f"{minutes:.0f} min" if minutes is not None else leg.status
                    print(f"        {shown:>8}  {entity.name}")
                for warning in routes.warnings:
                    print(f"        note: {warning}")

            # --- 4. store the shortlists ----------------------------------
            banner("4.", "Persist the shortlists  (acceptance: store shortlist)")
            shortlist_ops = []

            for key, ranked in shortlists.items():
                options = []
                for item in ranked:
                    entity = by_place_id.get(item.place.place_id)
                    if entity is None:
                        continue
                    minutes = walk_minutes.get(entity.entity_id)

                    pros = list(item.pros)
                    cons = list(item.cons)
                    if minutes is not None:
                        (pros if minutes <= 12 else cons).append(
                            f"{minutes:.0f} minutes' walk from the area centre (Google Routes)"
                        )

                    dimensions = dict(item.score.dimensions)
                    if minutes is not None:
                        dimensions["walk_minutes"] = round(minutes, 1)

                    basis = item.score.notes or "ranked on rating, review count and proximity"

                    options.append(
                        {
                            "option_id": new_id("opt"),
                            "data": {
                                "entity_id": entity.entity_id,
                                "purpose": key.split("_")[0],
                                "why": f"{entity.name}: {basis}",
                            },
                            "status": "shortlisted",
                            "score": {
                                "total": item.score.total,
                                "dimensions": dimensions,
                                "notes": item.score.notes,
                            },
                            "pros": pros,
                            "cons": cons,
                        }
                    )

                if not options:
                    continue

                existing = state.decisions.place_shortlists.get(key)
                shortlist_ops.append(
                    PatchOperation(
                        op="set" if existing else "add",
                        path=f"/decisions/place_shortlists/{key}",
                        value={
                            "decision_id": existing.decision_id if existing else new_id("dec"),
                            "status": "shortlisted",
                            "options": options,
                            "rationale": (
                                "Ranked on Google rating, review-count confidence and walking "
                                "proximity, then verified with a details fetch."
                            ),
                        },
                    )
                )

            result = await repo.apply_patch(
                state.trip_id,
                TripPatch(
                    base_revision=state.revision,
                    reason="store ranked place shortlists per area and purpose",
                    actor="system",
                    operations=shortlist_ops,
                ),
            )
            if not result.applied:
                print(f"    patch rejected: {[e.code for e in result.errors]}")
                for error in result.errors:
                    print(f"        {error.message}  {error.details}")
                return 1
            state = result.state

            for key, decision in state.decisions.place_shortlists.items():
                print(
                    f"    {key:20} {len(decision.options)} options  "
                    f"(decision {decision.decision_id})"
                )
                for option in decision.options:
                    entity = state.entities[option.data.entity_id]
                    print(f"        {option.score.total:.3f}  {entity.name}")

            # --- summary --------------------------------------------------
            banner("", "Result")
            print(f"    trip_id     {state.trip_id}")
            print(f"    revision    {state.revision}")
            print(f"    entities    {len(state.entities)}  (was {entities_before} at start)")
            print(f"    shortlists  {len(state.decisions.place_shortlists)}")
            for warning in result.warnings:
                print(f"    warning     [{warning.code}] {warning.message}")

            if trip_id:
                grew = len(state.entities) - entities_before
                verdict = "IDEMPOTENT" if grew == 0 else f"NOT IDEMPOTENT (+{grew} entities)"
                print(f"\n    re-run check: {verdict}")

            print("\n    Re-run against this trip:")
            print(
                f"      .\\.venv\\Scripts\\python.exe scripts\\build_tokyo_trip.py "
                f"--trip-id {state.trip_id}"
            )
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trip-id", default=None, help="reuse an existing trip")
    args = parser.parse_args()

    try:
        sys.exit(asyncio.run(main(args.trip_id)))
    except MissingCredentials as exc:
        sys.exit(f"\n{exc}\n")
