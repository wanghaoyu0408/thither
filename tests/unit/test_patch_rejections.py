from app.models import PatchOperation, RejectionRecord, TripPatch
from app.services import apply_patch
from tests.conftest import make_entity, sample_state


def patch(*operations: PatchOperation, base: int = 0, **kwargs) -> TripPatch:
    return TripPatch(base_revision=base, reason="test", operations=list(operations), **kwargs)


def rejecting_state(target_id: str = "ent_ramen", **kwargs):
    state = sample_state()
    state.rejections = [
        RejectionRecord(
            rejection_id="rej_1",
            target_kind=kwargs.get("target_kind", "entity"),
            target_id=target_id,
            label=kwargs.get("label", "Ichiran Shibuya"),
            reason=kwargs.get("reason", "too touristy"),
        )
    ]
    return state


def test_reintroducing_a_rejected_entity_is_refused():
    state = rejecting_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="add",
                path="/entities/ent_ramen",
                value=make_entity("ent_ramen", "Ichiran Shibuya").model_dump(mode="json"),
            )
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["REJECTION_VIOLATION"]
    assert "Ichiran Shibuya" in result.errors[0].message
    assert "too touristy" in result.errors[0].message
    assert result.errors[0].details == ["ent_ramen"]


def test_scheduling_a_rejected_entity_is_refused():
    state = rejecting_state()
    state.entities["ent_ramen"] = make_entity("ent_ramen", "Ichiran Shibuya")

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="add",
                path="/itinerary/days/0/items/-",
                value={
                    "item_id": "item_ramen",
                    "type": "restaurant",
                    "title": "Ramen",
                    "entity_id": "ent_ramen",
                },
            )
        ),
    )

    # The entity was already in the registry, so only the itinerary reference is
    # new - but it is still a re-introduction of a rejected place.
    assert result.applied is False
    assert [e.code for e in result.errors] == ["REJECTION_VIOLATION"]


def test_explicit_reconsideration_is_allowed():
    state = rejecting_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="add",
                path="/entities/ent_ramen",
                value=make_entity("ent_ramen", "Ichiran Shibuya").model_dump(mode="json"),
            ),
            allow_rejected=["ent_ramen"],
        ),
    )

    assert result.applied is True
    assert "ent_ramen" in result.state.entities


def test_recording_a_rejection_does_not_invalidate_existing_state():
    """Rejecting something already in the trip must not make the patch fail."""
    state = sample_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="add",
                path="/rejections/-",
                value={
                    "rejection_id": "rej_cafe",
                    "target_kind": "entity",
                    "target_id": "ent_cafe",
                    "label": "Fuglen Tokyo",
                    "reason": "we went last time",
                },
            )
        ),
    )

    assert result.applied is True
    assert result.state.entities["ent_cafe"].name == "Fuglen Tokyo"


def test_unrelated_changes_are_unaffected_by_rejections():
    state = rejecting_state()

    result = apply_patch(state, patch(PatchOperation(op="set", path="/status", value="planning")))

    assert result.applied is True


def test_shortlisting_a_rejected_option_is_refused():
    state = rejecting_state(target_id="opt_osaka", target_kind="decision_option", label="Osaka")

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="set", path="/decisions/destination/options/1/status", value="shortlisted"
            )
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["REJECTION_VIOLATION"]


def test_candidate_status_does_not_count_as_a_recommendation():
    """A rejected option may sit in the option list as long as it is not promoted."""
    state = rejecting_state(target_id="opt_osaka", target_kind="decision_option", label="Osaka")

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="set", path="/decisions/destination/options/1/status", value="rejected"
            )
        ),
    )

    assert result.applied is True
