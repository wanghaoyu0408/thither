from app.models import LockRecord, PatchOperation, TripPatch
from app.services import apply_patch
from tests.conftest import DAY_ONE, sample_state


def locked_state(**lock_kwargs) -> tuple:
    state = sample_state()
    lock = LockRecord(
        lock_id="lock_1",
        target_kind=lock_kwargs.get("target_kind", "itinerary_item"),
        target_id=lock_kwargs.get("target_id", "item_dinner"),
        reason=lock_kwargs.get("reason", "reservation already made"),
    )
    state.locks = [lock]
    return state, lock


def patch(*operations: PatchOperation, base: int = 0, **kwargs) -> TripPatch:
    return TripPatch(base_revision=base, reason="test", operations=list(operations), **kwargs)


def test_modifying_a_locked_item_is_rejected():
    state, lock = locked_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(op="set", path="/itinerary/days/0/items/1/title", value="Somewhere else")
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["LOCK_VIOLATION"]
    assert result.errors[0].lock_id == lock.lock_id
    assert "reservation already made" in result.errors[0].message


def test_removing_a_locked_item_is_rejected():
    state, _ = locked_state()

    result = apply_patch(
        state, patch(PatchOperation(op="remove", path="/itinerary/days/0/items/1"))
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["LOCK_VIOLATION"]
    assert "was removed" in result.errors[0].message


def test_editing_a_different_item_in_the_same_day_is_allowed():
    state, _ = locked_state()

    result = apply_patch(
        state,
        patch(PatchOperation(op="set", path="/itinerary/days/0/items/0/title", value="Museum")),
    )

    assert result.applied is True
    assert result.state.itinerary.days[0].items[0].title == "Museum"


def test_lock_survives_reordering_because_it_addresses_an_id():
    """Removing an earlier sibling shifts indices; the lock must still bind."""
    state, _ = locked_state()

    shifted = apply_patch(
        state, patch(PatchOperation(op="remove", path="/itinerary/days/0/items/0"))
    )
    assert shifted.applied is True
    assert shifted.state.itinerary.days[0].items[0].item_id == "item_dinner"

    # The locked item is now at index 0; editing it must still be refused.
    result = apply_patch(
        shifted.state,
        patch(
            PatchOperation(op="set", path="/itinerary/days/0/items/0/title", value="Moved"),
            base=1,
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["LOCK_VIOLATION"]


def test_explicit_unlock_permits_the_change():
    state, lock = locked_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="set", path="/itinerary/days/0/items/1/title", value="Somewhere else"
            ),
            unlock_targets=[lock.lock_id],
        ),
    )

    assert result.applied is True
    assert result.state.itinerary.days[0].items[1].title == "Somewhere else"
    assert result.state.locks == []


def test_deleting_a_lock_record_without_unlocking_is_rejected():
    """Otherwise a patch could drop the guard now and edit freely next turn."""
    state, _ = locked_state()

    result = apply_patch(state, patch(PatchOperation(op="remove", path="/locks/0")))

    assert result.applied is False
    assert [e.code for e in result.errors] == ["LOCK_VIOLATION"]
    assert "without naming it in unlock_targets" in result.errors[0].message


def test_locking_a_day_protects_its_items():
    state, _ = locked_state(target_kind="itinerary_day", target_id=DAY_ONE.isoformat())

    result = apply_patch(
        state,
        patch(PatchOperation(op="set", path="/itinerary/days/0/items/0/title", value="Changed")),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["LOCK_VIOLATION"]


def test_locking_a_decision_protects_its_selection():
    state, _ = locked_state(target_kind="decision", target_id="dec_dest")

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="set", path="/decisions/destination/selected_option_id", value="opt_osaka"
            )
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["LOCK_VIOLATION"]


def test_locking_an_entity_protects_its_facts():
    state, _ = locked_state(target_kind="entity", target_id="ent_cafe")

    result = apply_patch(
        state, patch(PatchOperation(op="set", path="/entities/ent_cafe/rating", value=1.0))
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["LOCK_VIOLATION"]


def test_adding_a_new_lock_is_allowed():
    state = sample_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="add",
                path="/locks/-",
                value={
                    "lock_id": "lock_new",
                    "target_kind": "itinerary_item",
                    "target_id": "item_dinner",
                    "reason": "booked it",
                },
            )
        ),
    )

    assert result.applied is True
    assert [lock.lock_id for lock in result.state.locks] == ["lock_new"]
