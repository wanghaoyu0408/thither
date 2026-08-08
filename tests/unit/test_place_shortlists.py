"""Regression tests for two bugs that `place_shortlists` exposed.

Lock enforcement and rejection memory each walked `state["decisions"].values()`
assuming every value *is* a decision. A dict of decisions broke both at once,
and silently: shortlisted decisions became unlockable, and shortlisted options
became invisible to rejection memory - which is precisely the case the feature
exists to serve, since restaurants are what users reject.
"""

from app.models import (
    Decision,
    DecisionOption,
    LockRecord,
    PatchOperation,
    PlaceOption,
    RejectionRecord,
    TripPatch,
)
from app.services import apply_patch, check_integrity, collect_lock_targets
from tests.conftest import make_entity, sample_state


def patch(*operations: PatchOperation, base: int = 0, **kwargs) -> TripPatch:
    return TripPatch(base_revision=base, reason="test", operations=list(operations), **kwargs)


def state_with_shortlist():
    state = sample_state()
    state.entities["ent_ramen"] = make_entity("ent_ramen", "Ichiran Shibuya")
    state.decisions.place_shortlists["dinner_day1"] = Decision[PlaceOption](
        decision_id="dec_dinner1",
        status="shortlisted",
        options=[
            DecisionOption[PlaceOption](
                option_id="opt_cafe",
                data=PlaceOption(entity_id="ent_cafe", purpose="dinner"),
                status="shortlisted",
            ),
            DecisionOption[PlaceOption](
                option_id="opt_ramen",
                data=PlaceOption(entity_id="ent_ramen", purpose="dinner"),
                status="candidate",
            ),
        ],
    )
    return state


# --- the walker itself -------------------------------------------------------


def test_nested_decisions_are_discovered():
    state = state_with_shortlist()

    targets = collect_lock_targets(state.model_dump(mode="json"))

    assert ("decision", "dec_dinner1") in targets
    assert ("decision", "dec_dest") in targets


def test_iter_decisions_names_shortlists():
    names = [name for name, _ in state_with_shortlist().decisions.iter_decisions()]

    assert "destination" in names
    assert "place_shortlists.dinner_day1" in names


# --- locks -------------------------------------------------------------------


def test_a_shortlist_decision_is_genuinely_lockable():
    state = state_with_shortlist()
    state.locks = [
        LockRecord(
            lock_id="lock_dinner",
            target_kind="decision",
            target_id="dec_dinner1",
            reason="the group already agreed on this list",
        )
    ]

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="set",
                path="/decisions/place_shortlists/dinner_day1/options/0/status",
                value="rejected",
            )
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["LOCK_VIOLATION"]
    assert result.errors[0].lock_id == "lock_dinner"


def test_unlocking_still_works_for_shortlists():
    state = state_with_shortlist()
    state.locks = [
        LockRecord(
            lock_id="lock_dinner",
            target_kind="decision",
            target_id="dec_dinner1",
            reason="agreed",
        )
    ]

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="set",
                path="/decisions/place_shortlists/dinner_day1/rationale",
                value="revisited after the group chat",
            ),
            unlock_targets=["lock_dinner"],
        ),
    )

    assert result.applied is True


# --- rejection memory --------------------------------------------------------


def test_promoting_a_rejected_place_inside_a_shortlist_is_refused():
    state = state_with_shortlist()
    state.rejections = [
        RejectionRecord(
            rejection_id="rej_1",
            target_kind="decision_option",
            target_id="opt_ramen",
            label="Ichiran Shibuya",
            reason="too touristy",
        )
    ]

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="set",
                path="/decisions/place_shortlists/dinner_day1/options/1/status",
                value="shortlisted",
            )
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["REJECTION_VIOLATION"]
    assert "Ichiran Shibuya" in result.errors[0].message


def test_adding_a_whole_shortlist_containing_a_rejected_place_is_refused():
    state = sample_state()
    state.rejections = [
        RejectionRecord(
            rejection_id="rej_1",
            target_kind="decision_option",
            target_id="opt_ramen",
            label="Ichiran Shibuya",
        )
    ]

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="add",
                path="/decisions/place_shortlists/lunch_day2",
                value={
                    "decision_id": "dec_lunch2",
                    "status": "shortlisted",
                    "options": [
                        {
                            "option_id": "opt_ramen",
                            "data": {"entity_id": "ent_cafe", "purpose": "lunch"},
                            "status": "shortlisted",
                        }
                    ],
                },
            )
        ),
    )

    assert result.applied is False
    assert [e.code for e in result.errors] == ["REJECTION_VIOLATION"]


# --- integrity ---------------------------------------------------------------


def test_shortlist_pointing_at_a_missing_entity_is_caught():
    state = state_with_shortlist()
    state.decisions.place_shortlists["dinner_day1"].options[0].data.entity_id = "ent_ghost"

    problems = check_integrity(state)

    assert any("references unknown entity 'ent_ghost'" in problem for problem in problems)


def test_a_healthy_shortlist_passes_integrity():
    assert check_integrity(state_with_shortlist()) == []


def test_shortlists_survive_a_serialization_round_trip():
    state = state_with_shortlist()

    from app.models import TripState

    restored = TripState.model_validate(state.model_dump(mode="json"))

    assert restored == state
    assert restored.decisions.place_shortlists["dinner_day1"].options[0].data.purpose == "dinner"


def test_adding_a_shortlist_through_a_patch_works_end_to_end():
    state = sample_state()

    result = apply_patch(
        state,
        patch(
            PatchOperation(
                op="add",
                path="/decisions/place_shortlists/dinner_day1",
                value={
                    "decision_id": "dec_dinner1",
                    "status": "shortlisted",
                    "options": [
                        {
                            "option_id": "opt_cafe",
                            "data": {
                                "entity_id": "ent_cafe",
                                "purpose": "dinner",
                                "why": "4.3 from 2,300 reviews, five minutes from the hotel",
                            },
                            "status": "shortlisted",
                            "score": {"total": 0.81, "dimensions": {"rating": 0.65}},
                        }
                    ],
                },
            )
        ),
    )

    assert result.applied is True
    shortlist = result.state.decisions.place_shortlists["dinner_day1"]
    assert shortlist.options[0].data.entity_id == "ent_cafe"
    assert shortlist.options[0].score.total == 0.81
