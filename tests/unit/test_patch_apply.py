import pytest

from app.models import PatchOperation, TripPatch
from app.services import apply_patch
from tests.conftest import sample_state


def patch(*operations: PatchOperation, base: int = 0, **kwargs) -> TripPatch:
    return TripPatch(base_revision=base, reason="test", operations=list(operations), **kwargs)


def test_set_applies_and_bumps_revision():
    state = sample_state()

    result = apply_patch(state, patch(PatchOperation(op="set", path="/status", value="planning")))

    assert result.applied is True
    assert result.revision == 1
    assert result.state.status == "planning"
    # The caller's object is untouched; the new state is returned.
    assert state.status == "draft"
    assert state.revision == 0


def test_add_appends_to_a_list():
    state = sample_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="add",
                path="/constraints/-",
                value={
                    "id": "con_shellfish",
                    "category": "food",
                    "description": "Alice has a shellfish allergy",
                    "type": "hard",
                    "scope": "traveler",
                    "traveler_id": "trv_b",
                    "source": "user_explicit",
                    "confirmed": True,
                },
            )
        ),
    )

    assert result.applied is True
    assert [c.id for c in result.state.constraints] == ["con_shellfish"]


def test_remove_deletes_an_item():
    state = sample_state()

    result = apply_patch(
        state, patch(PatchOperation(op="remove", path="/itinerary/days/0/items/1"))
    )

    assert result.applied is True
    assert [i.item_id for i in result.state.itinerary.days[0].items] == ["item_museum"]


def test_revision_conflict_is_reported_not_applied():
    state = sample_state()

    result = apply_patch(
        state, patch(PatchOperation(op="set", path="/status", value="planning"), base=7)
    )

    assert result.applied is False
    assert result.revision == 0
    assert [e.code for e in result.errors] == ["REVISION_CONFLICT"]


@pytest.mark.parametrize(
    "path",
    ["/trip_id", "/revision", "/schema_version", "/metadata/created_at"],
)
def test_protected_paths_rejected(path):
    state = sample_state()

    result = apply_patch(state, patch(PatchOperation(op="set", path=path, value="hacked")))

    assert result.applied is False
    assert [e.code for e in result.errors] == ["PROTECTED_PATH"]
    assert result.errors[0].path == path
    assert result.errors[0].op_index == 0


def test_schema_invalid_value_rejected_with_detail():
    state = sample_state()

    result = apply_patch(
        state, patch(PatchOperation(op="set", path="/status", value="teleporting"))
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["SCHEMA_INVALID"]
    assert any("status" in detail for detail in result.errors[0].details)


def test_nested_schema_violation_rejected():
    state = sample_state()

    result = apply_patch(
        state,
        patch(PatchOperation(op="set", path="/itinerary/days/0/items/0/type", value="teleport")),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["SCHEMA_INVALID"]


def test_invalid_pointer_names_the_operation():
    state = sample_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(op="set", path="/status", value="planning"),
            PatchOperation(op="set", path="/nowhere/deep", value=1),
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["INVALID_POINTER"]
    assert result.errors[0].op_index == 1


def test_patch_is_atomic_when_a_later_operation_fails():
    state = sample_state()
    before = state.model_dump(mode="json")

    result = apply_patch(
        state,
        patch(
            PatchOperation(op="set", path="/status", value="planning"),
            PatchOperation(op="set", path="/brief/pace", value="relaxed"),
            PatchOperation(op="remove", path="/itinerary/days/9"),
        ),
    )

    assert result.applied is False
    assert result.revision == 0
    assert result.state is None
    assert state.model_dump(mode="json") == before


def test_unverifiable_hard_constraint_warns_rather_than_passing_silently():
    state = sample_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="add",
                path="/constraints/-",
                value={
                    "id": "con_nonstop",
                    "category": "flight",
                    "description": "must be nonstop",
                    "type": "hard",
                    "scope": "trip",
                    "source": "user_explicit",
                    "confirmed": True,
                },
            )
        ),
    )

    assert result.applied is True
    warning = next(w for w in result.warnings if w.constraint_id == "con_nonstop")
    assert warning.code == "CONSTRAINT_NOT_CHECKABLE"
    assert "flight" in warning.message


def test_unknown_unlock_target_warns_but_applies():
    state = sample_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(op="set", path="/status", value="planning"),
            unlock_targets=["lock_does_not_exist"],
        ),
    )

    assert result.applied is True
    assert any(w.code == "UNKNOWN_UNLOCK_TARGET" for w in result.warnings)


def test_metadata_created_at_survives_while_updated_at_moves():
    state = sample_state()
    original_created = state.metadata.created_at

    result = apply_patch(state, patch(PatchOperation(op="set", path="/status", value="planning")))

    assert result.state.metadata.created_at == original_created
    assert result.state.metadata.updated_at >= state.metadata.updated_at
