"""Persistence for trips and profiles.

The interesting part is `TripRepository.apply_patch`: optimistic concurrency
lives in the SQL `WHERE revision = :base_revision`, not just in the Python
check, so two clients patching the same base cannot both win.
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TravelerProfileRow, TripEventRow, TripRow
from app.models.common import utcnow
from app.models.patch import PatchError, PatchOperation, PatchResult, TripPatch
from app.models.traveler import TravelerProfile
from app.models.trip import TripState
from app.services.patch_service import apply_patch as apply_patch_to_state


class RepositoryError(Exception):
    pass


class TripNotFound(RepositoryError):
    pass


class ProfileNotFound(RepositoryError):
    pass


# Maps a patched path onto a readable audit event, so `GET /trips/{id}/events`
# reads as a trip history rather than a pile of JSON pointers.
_EVENT_RULES: tuple[tuple[str, str, str], ...] = (
    ("/constraints", "add", "constraint_added"),
    ("/constraints", "remove", "constraint_removed"),
    ("/locks", "add", "lock_added"),
    ("/locks", "remove", "lock_removed"),
    ("/rejections", "add", "rejection_added"),
    ("/entities", "add", "entity_added"),
    ("/entities", "set", "entity_updated"),
    ("/entities", "remove", "entity_removed"),
    ("/itinerary", "*", "itinerary_updated"),
    ("/decisions/place_shortlists", "*", "shortlist_updated"),
    ("/decisions/destination", "*", "destination_updated"),
    ("/decisions/flights", "*", "flights_updated"),
    ("/decisions/hotel_area", "*", "hotel_area_updated"),
    ("/decisions/hotel", "*", "hotel_updated"),
    ("/brief", "*", "brief_updated"),
    ("/travelers", "*", "travelers_updated"),
)


def _derive_event_types(operations: list[PatchOperation]) -> list[str]:
    found: list[str] = []
    for op in operations:
        for prefix, kind, event_type in _EVENT_RULES:
            if not (op.path == prefix or op.path.startswith(f"{prefix}/")):
                continue
            if kind not in ("*", op.op):
                continue
            if event_type not in found:
                found.append(event_type)
    return found


class ProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, profile: TravelerProfile) -> TravelerProfile:
        self._session.add(
            TravelerProfileRow(id=profile.profile_id, profile=profile.model_dump(mode="json"))
        )
        await self._session.commit()
        return profile

    async def get(self, profile_id: str) -> TravelerProfile:
        row = await self._session.get(TravelerProfileRow, profile_id)
        if row is None:
            raise ProfileNotFound(profile_id)
        return TravelerProfile.model_validate(row.profile)

    async def list_profiles(self, limit: int = 50, offset: int = 0) -> list[TravelerProfile]:
        result = await self._session.execute(
            select(TravelerProfileRow)
            .order_by(TravelerProfileRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [TravelerProfile.model_validate(row.profile) for row in result.scalars()]

    async def update(self, profile_id: str, changes: dict[str, Any]) -> TravelerProfile:
        row = await self._session.get(TravelerProfileRow, profile_id)
        if row is None:
            raise ProfileNotFound(profile_id)

        # Pydantic ignores unknown keys, which would turn a typo into a silent no-op.
        unknown = sorted(set(changes) - set(TravelerProfile.model_fields))
        if unknown:
            raise ValueError(f"unknown profile fields: {unknown}")

        merged = {**row.profile, **changes}
        merged["profile_id"] = profile_id
        merged["updated_at"] = utcnow().isoformat()
        # Bumped here rather than by the caller: a trip snapshots preferences and
        # names the revision it took them from, so the version has to move on
        # every stored edit whether or not anyone remembered to say so.
        merged["revision"] = int(row.profile.get("revision", 0)) + 1

        profile = TravelerProfile.model_validate(merged)
        row.profile = profile.model_dump(mode="json")
        await self._session.commit()
        return profile


class TripRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, state: TripState) -> TripState:
        self._session.add(
            TripRow(
                id=state.trip_id,
                revision=state.revision,
                state=state.model_dump(mode="json"),
            )
        )
        await self._session.flush()
        self._session.add(
            TripEventRow(
                trip_id=state.trip_id,
                revision=state.revision,
                event_type="trip_created",
                payload={"title": state.metadata.title, "created_by": state.metadata.created_by},
            )
        )
        await self._session.commit()
        return state

    async def get(self, trip_id: str) -> TripState:
        row = await self._session.get(TripRow, trip_id)
        if row is None:
            raise TripNotFound(trip_id)
        return TripState.model_validate(row.state)

    async def list_trips(self, limit: int = 50, offset: int = 0) -> list[TripState]:
        result = await self._session.execute(
            select(TripRow).order_by(TripRow.updated_at.desc()).limit(limit).offset(offset)
        )
        return [TripState.model_validate(row.state) for row in result.scalars()]

    async def list_events(self, trip_id: str, limit: int = 200) -> list[dict[str, Any]]:
        if await self._session.get(TripRow, trip_id) is None:
            raise TripNotFound(trip_id)

        result = await self._session.execute(
            select(TripEventRow)
            .where(TripEventRow.trip_id == trip_id)
            .order_by(TripEventRow.id)
            .limit(limit)
        )
        return [
            {
                "id": row.id,
                "revision": row.revision,
                "event_type": row.event_type,
                "payload": row.payload,
                "created_at": row.created_at,
            }
            for row in result.scalars()
        ]

    async def apply_patch(self, trip_id: str, patch: TripPatch) -> PatchResult:
        """Validate the patch against current state, then commit it conditionally."""
        state = await self.get(trip_id)

        result = apply_patch_to_state(state, patch)
        if not result.applied or result.state is None:
            return result

        new_state = result.state

        # The WHERE clause is the concurrency guard: if another writer moved the
        # revision after our read, this matches zero rows.
        outcome = await self._session.execute(
            update(TripRow)
            .where(TripRow.id == trip_id, TripRow.revision == patch.base_revision)
            .values(
                revision=new_state.revision,
                state=new_state.model_dump(mode="json"),
                updated_at=utcnow(),
            )
        )

        if outcome.rowcount == 0:
            await self._session.rollback()
            current = await self._session.scalar(
                select(TripRow.revision).where(TripRow.id == trip_id)
            )
            return PatchResult(
                applied=False,
                revision=current if current is not None else patch.base_revision,
                errors=[
                    PatchError(
                        code="REVISION_CONFLICT",
                        message=(
                            f"another change landed while this patch was being validated; "
                            f"the trip is now at revision {current}"
                        ),
                    )
                ],
            )

        for event_type in [*_derive_event_types(patch.operations), "patch_applied"]:
            self._session.add(
                TripEventRow(
                    trip_id=trip_id,
                    revision=new_state.revision,
                    event_type=event_type,
                    payload={
                        "reason": patch.reason,
                        "actor": patch.actor,
                        "operations": [op.model_dump(mode="json") for op in patch.operations],
                        "warnings": [w.model_dump(mode="json") for w in result.warnings],
                    }
                    if event_type == "patch_applied"
                    else {"reason": patch.reason, "actor": patch.actor},
                )
            )

        await self._session.commit()
        return result
