"""The invariants, and one test per defect the live probes found.

Every case here corresponds to a row in INVARIANTS.md. The ones marked "live"
in that document were not caught by reasoning about the code - they were caught
by running it against real providers and a real model, which is why they are
pinned here rather than left to be rediscovered.
"""

import pytest

from app.models.common import as_attestation, as_attestation_map
from app.models.entity import PlaceEntity
from app.models.patch import TripPatch
from app.models.place import PlaceSummary
from app.providers.google_places import normalize_place
from app.services.entity_service import keep_attested, resolve_place, resolve_places
from app.services.scoring import combine, floor_check, ranking_value
from tests.conftest import make_entity, sample_state

# --- 1. absence is not negation ----------------------------------------------


def test_a_provider_positive_is_the_only_confirmation():
    """Google attests the positive case only; its False means "nobody said".

    Measured live: 8/8 Indian restaurants in Shinjuku attested, 1/8 Shibuya
    pizzerias - and every pizzeria serves a margherita.
    """
    attested = normalize_place({"id": "a", "servesVegetarianFood": True})
    denied = normalize_place({"id": "b", "servesVegetarianFood": False})
    silent = normalize_place({"id": "c"})

    assert attested.serves_vegetarian == "confirmed_true"
    # The load-bearing line: False maps to unknown, never confirmed_false.
    assert denied.serves_vegetarian == "unknown"
    assert silent.serves_vegetarian == "unknown"


def test_accessibility_keeps_only_positive_attestations():
    place = normalize_place(
        {
            "id": "a",
            "accessibilityOptions": {
                "wheelchairAccessibleEntrance": True,
                "wheelchairAccessibleParking": False,
            },
        }
    )

    assert place.accessibility == {"wheelchairAccessibleEntrance": "confirmed_true"}


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (True, "confirmed_true"),
        (False, "unknown"),
        (None, "unknown"),
        ("confirmed_true", "confirmed_true"),
        ("confirmed_false", "confirmed_false"),
        ("unknown", "unknown"),
    ],
)
def test_old_bool_json_still_validates(stored, expected):
    """Trips persisted before the tri-state carry true/false/null in this field.

    They have to keep loading, and they have to load with the meaning they
    originally had - True was the only trustworthy signal in that encoding.
    """
    entity = PlaceEntity.model_validate(
        {"entity_id": "e", "name": "x", "lat": 0.0, "lng": 0.0, "serves_vegetarian": stored}
    )
    assert entity.serves_vegetarian == expected

    summary = PlaceSummary.model_validate({"place_id": "p", "serves_vegetarian": stored})
    assert summary.serves_vegetarian == expected


def test_old_accessibility_json_still_validates():
    entity = PlaceEntity.model_validate(
        {
            "entity_id": "e",
            "name": "x",
            "lat": 0.0,
            "lng": 0.0,
            "accessibility": {"wheelchairAccessibleEntrance": True, "other": False},
        }
    )

    assert entity.accessibility == {
        "wheelchairAccessibleEntrance": "confirmed_true",
        "other": "unknown",
    }


def test_as_attestation_is_total():
    assert as_attestation(True) == "confirmed_true"
    assert as_attestation(False) == "unknown"
    assert as_attestation(None) == "unknown"
    assert as_attestation("nonsense") == "unknown"
    assert as_attestation_map(None) == {}


def test_unknown_never_overwrites_a_confirmed_value():
    """A cheap search must not erase what an expensive details call established.

    RANKING-tier searches carry no dietary fields at all, so every one of their
    results is `unknown`. Letting that win would lose a fact to a call that
    simply did not ask.
    """
    assert keep_attested("unknown", "confirmed_true") == "confirmed_true"
    assert keep_attested("unknown", "confirmed_false") == "confirmed_false"
    # A confirmed value in either direction is fresher data and does win.
    assert keep_attested("confirmed_false", "confirmed_true") == "confirmed_false"
    assert keep_attested("confirmed_true", "unknown") == "confirmed_true"


def test_a_later_search_keeps_an_earlier_attestation():
    known = PlaceEntity(
        entity_id="ent_a",
        name="Veg Place",
        lat=1.0,
        lng=1.0,
        provider_refs={"google_place_id": "ChIJ_a"},
        serves_vegetarian="confirmed_true",
        accessibility={"wheelchairAccessibleEntrance": "confirmed_true"},
    )
    # A RANKING-tier result: no dietary fields requested, so all unknown.
    plain = PlaceSummary(place_id="ChIJ_a", name="Veg Place", lat=1.0, lng=1.0)

    merged = resolve_place(plain, {known.entity_id: known})

    assert merged.serves_vegetarian == "confirmed_true"
    assert merged.accessibility["wheelchairAccessibleEntrance"] == "confirmed_true"


def test_a_confirmed_denial_is_recorded_when_a_provider_makes_one():
    """No current provider emits it; the tri-state exists so one could."""
    known = PlaceEntity(
        entity_id="ent_a",
        name="Steak House",
        lat=1.0,
        lng=1.0,
        provider_refs={"google_place_id": "ChIJ_a"},
        serves_vegetarian="confirmed_true",
    )
    denial = PlaceSummary(place_id="ChIJ_a", serves_vegetarian="confirmed_false")

    merged = resolve_places([denial], {known.entity_id: known})[0]

    assert merged.serves_vegetarian == "confirmed_false"


# --- 2. score is not confidence ----------------------------------------------


def test_a_stored_score_is_never_damped():
    """`total` says what the evidence said; `coverage` says how much there was."""
    thin = combine({"price": (1.0, 1.0), "rating": (None, 3.0)})

    assert thin.total == 1.0
    assert thin.coverage == 0.25
    # The discount exists, but only at ordering time.
    assert ranking_value(thin) < thin.total


def test_ordering_discounts_thin_evidence():
    """The live shape: a flawless score on one dimension against a good one on all.

    The discount is proportional, not absolute - a thin option can still win if
    it is far enough ahead. What it cannot do is win on a hair's breadth while
    nobody has looked at it, which is what happened to the 3-review hotel.
    """
    thin = combine({"price": (1.0, 1.0), "rating": (None, 3.0)})
    solid = combine({"price": (0.8, 1.0), "rating": (0.8, 3.0)})

    assert thin.total > solid.total
    assert ranking_value(thin) < ranking_value(solid)


def test_full_coverage_orders_on_the_score_itself():
    scored = combine({"a": (0.62, 1.0), "b": (0.62, 1.0)})

    assert scored.coverage == 1.0
    assert ranking_value(scored) == scored.total


@pytest.mark.parametrize(
    ("value", "reviews", "expected"),
    [
        (4.6, 900, "confirmed_true"),
        (3.1, 900, "confirmed_false"),
        # Too thin to score on, therefore too thin to clear a floor.
        (5.0, 3, "unknown"),
        (None, None, "unknown"),
    ],
)
def test_a_floor_is_answered_in_the_tri_state(value, reviews, expected):
    """Sparse data cannot settle a factual claim in either direction.

    The live defect: 5.0-from-3-reviews was refused as a score and accepted as
    proof of a stated 4.5 floor - untrustworthy and authoritative at once.
    """
    assert floor_check(value, reviews, 4.5, min_reviews=20) == expected


def test_places_now_report_coverage():
    """score_place used to renormalize inline, so places had no coverage at all."""
    from app.services.ranking_service import score_place

    sparse = score_place(PlaceSummary(place_id="p", rating=4.5))

    assert 0.0 < sparse.score.coverage < 1.0
    assert "no data for" in sparse.score.notes


# --- 3. conflicts cannot be moved by tuning a ranker -------------------------


def test_conflict_detection_cannot_reach_any_ranking_code():
    """A structural guarantee, not a promise.

    If tuning a weight could change which disagreements the group is *told
    about*, the ranker would quietly be deciding what people argue over - the
    averaging failure wearing a different hat. Checked by walking the import
    graph, so it holds transitively rather than only at the top of the file.
    """
    import ast
    import pathlib

    reachable: set[str] = set()
    stack = ["app.services.conflict_service"]
    while stack:
        module = stack.pop()
        if module in reachable:
            continue
        reachable.add(module)
        path = pathlib.Path(module.replace(".", "/") + ".py")
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
                stack.append(node.module)

    ranking_modules = {
        "app.services.scoring",
        "app.services.group_scoring",
        "app.services.ranking_service",
        "app.services.hotel_ranking",
        "app.services.flight_ranking",
    }
    assert not (ranking_modules & reachable), (
        "conflict detection can now reach ranking code; a scoring change could move "
        "which conflicts are reported"
    )


# --- 4. the group formula is configuration, not doctrine ---------------------


def test_the_worst_weight_moves_the_total_and_is_recorded():
    from app.services.group_scoring import build_group_score

    scores = {"a": 1.0, "b": 0.0}

    mean_only = build_group_score(scores, worst_weight=0.0)
    balanced = build_group_score(scores, worst_weight=0.4)
    maximin = build_group_score(scores, worst_weight=1.0)

    assert mean_only.total == 0.5
    assert balanced.total == pytest.approx(0.3)
    assert maximin.total == 0.0
    # A stored score is self-describing: 0.3 means nothing without the weight.
    assert balanced.worst_weight == 0.4
    assert maximin.worst_weight == 1.0


def test_the_setting_is_bounded():
    from pydantic import ValidationError

    from app.config import Settings

    assert Settings().group_worst_weight == 0.4
    with pytest.raises(ValidationError):
        Settings(group_worst_weight=1.5)


# --- 5. atomicity, and success only after reload -----------------------------


def entity_op(entity_id: str) -> dict:
    entity = make_entity(entity_id, f"Place {entity_id}")
    return {"op": "add", "path": f"/entities/{entity_id}", "value": entity.model_dump(mode="json")}


async def test_a_batch_is_all_or_nothing(session):
    """The live window: a refresh landed and the replan behind it was rejected.

    Both patches are valid on their own; the second breaks referential
    integrity. Neither may persist.
    """
    from app.db.repository import TripRepository

    repository = TripRepository(session)
    stored = await repository.create(sample_state())

    good = TripPatch(
        base_revision=stored.revision,
        reason="store a place",
        actor="agent",
        operations=[entity_op("ent_new")],
    )
    bad = TripPatch(
        base_revision=stored.revision + 1,
        reason="schedule something that does not exist",
        actor="agent",
        operations=[
            {
                "op": "add",
                "path": "/itinerary/days/0/items/-",
                "value": {
                    "item_id": "item_ghost",
                    "type": "restaurant",
                    "entity_id": "ent_missing",
                    "title": "Ghost",
                },
            }
        ],
    )

    results = await repository.apply_patches(stored.trip_id, [good, bad])

    assert results[0].applied
    assert not results[-1].applied

    # Nothing landed: not the revision, not the entity from the first patch.
    reloaded = await repository.get(stored.trip_id)
    assert reloaded.revision == stored.revision
    assert "ent_new" not in reloaded.entities


async def test_a_whole_batch_persists_together(session):
    from app.db.repository import TripRepository

    repository = TripRepository(session)
    stored = await repository.create(sample_state())

    patches = [
        TripPatch(
            base_revision=stored.revision + offset,
            reason=f"patch {offset}",
            actor="agent",
            operations=[entity_op(f"ent_{offset}")],
        )
        for offset in range(3)
    ]

    results = await repository.apply_patches(stored.trip_id, patches)

    assert all(result.applied for result in results)
    reloaded = await repository.get(stored.trip_id)
    assert reloaded.revision == stored.revision + 3
    assert {"ent_0", "ent_1", "ent_2"} <= set(reloaded.entities)


async def test_success_is_reported_from_the_reloaded_row(session):
    """A revision that was never read back is a claim, not a fact."""
    from app.db.repository import TripRepository

    repository = TripRepository(session)
    stored = await repository.create(sample_state())

    result = await repository.apply_patch(
        stored.trip_id,
        TripPatch(
            base_revision=stored.revision,
            reason="store a place",
            actor="agent",
            operations=[entity_op("ent_new")],
        ),
    )

    persisted = await repository.get(stored.trip_id)
    assert result.applied
    assert result.revision == persisted.revision
    # The returned state *is* the persisted one, not the in-memory candidate.
    assert result.state.model_dump(mode="json") == persisted.model_dump(mode="json")


async def test_a_stale_base_revision_lands_nothing(session):
    from app.db.repository import TripRepository

    repository = TripRepository(session)
    stored = await repository.create(sample_state())

    await repository.apply_patch(
        stored.trip_id,
        TripPatch(
            base_revision=stored.revision,
            reason="first writer",
            actor="agent",
            operations=[entity_op("ent_first")],
        ),
    )

    # A second writer that read the trip before the first one landed.
    results = await repository.apply_patches(
        stored.trip_id,
        [
            TripPatch(
                base_revision=stored.revision,
                reason="second writer",
                actor="agent",
                operations=[entity_op("ent_second")],
            )
        ],
    )

    assert not results[0].applied
    assert results[0].errors[0].code == "REVISION_CONFLICT"
    reloaded = await repository.get(stored.trip_id)
    assert "ent_second" not in reloaded.entities


async def test_a_rejected_batch_leaves_the_turns_staged_work_intact(session):
    """The runner path: nothing persisted means nothing thrown away either."""
    from app.agent.runner import AgentRun, AgentRunner
    from app.agent.tool_registry import ToolContext
    from app.config import Settings
    from app.db.repository import TripRepository
    from app.services.proposal_store import ProposalStore

    repository = TripRepository(session)
    stored = await repository.create(sample_state())

    settings = Settings(openai_api_key="test", database_url="sqlite+aiosqlite:///:memory:")
    runner = AgentRunner(llm=None, toolbox=None, session=session, settings=settings)
    context = ToolContext(state=stored, toolbox=None, proposals=ProposalStore(), settings=settings)
    context.pending_entity_ops.append(make_entity("ent_pending", "Pending"))

    run = AgentRun(
        trip_id=stored.trip_id, revision_before=stored.revision, revision_after=stored.revision
    )
    outcome = await runner._apply_all(
        context,
        run,
        [
            {"operations": [entity_op("ent_one")], "scope": None, "reason": "first"},
            {
                "operations": [{"op": "set", "path": "/status", "value": "not-a-status"}],
                "scope": None,
                "reason": "second",
            },
        ],
    )

    assert outcome["applied"] is False
    reloaded = await repository.get(stored.trip_id)
    assert reloaded.revision == stored.revision
    assert "ent_one" not in reloaded.entities
    # The turn's work is still staged, because it was never consumed.
    assert context.pending_entity_ops
    assert context.state.revision == stored.revision


async def test_a_successful_batch_reports_the_persisted_revision(session):
    from app.agent.runner import AgentRun, AgentRunner
    from app.agent.tool_registry import ToolContext
    from app.config import Settings
    from app.db.repository import TripRepository
    from app.services.proposal_store import ProposalStore

    repository = TripRepository(session)
    stored = await repository.create(sample_state())

    settings = Settings(openai_api_key="test", database_url="sqlite+aiosqlite:///:memory:")
    runner = AgentRunner(llm=None, toolbox=None, session=session, settings=settings)
    context = ToolContext(state=stored, toolbox=None, proposals=ProposalStore(), settings=settings)
    run = AgentRun(
        trip_id=stored.trip_id, revision_before=stored.revision, revision_after=stored.revision
    )

    outcome = await runner._apply_all(
        context,
        run,
        [
            {"operations": [entity_op("ent_a")], "scope": None, "reason": "first"},
            {"operations": [entity_op("ent_b")], "scope": None, "reason": "second"},
        ],
    )

    persisted = await repository.get(stored.trip_id)
    assert outcome["applied"] is True
    assert outcome["persisted"] is True
    assert outcome["revision"] == persisted.revision
    assert run.revision_after == persisted.revision
    assert context.state.revision == persisted.revision


async def test_the_audit_trail_records_every_patch_in_a_batch(session):
    from app.db.repository import TripRepository

    repository = TripRepository(session)
    stored = await repository.create(sample_state())

    await repository.apply_patches(
        stored.trip_id,
        [
            TripPatch(
                base_revision=stored.revision + offset,
                reason=f"patch {offset}",
                actor="agent",
                operations=[entity_op(f"ent_{offset}")],
            )
            for offset in range(2)
        ],
    )

    events = await repository.list_events(stored.trip_id)
    applied = [event for event in events if event["event_type"] == "patch_applied"]
    assert len(applied) == 2
    assert [event["revision"] for event in applied] == [stored.revision + 1, stored.revision + 2]


# --- 6. things only a live run found -----------------------------------------


def test_the_prompt_tells_the_model_to_apply_its_proposal():
    """It generated twice and stopped, having been told nowhere to commit."""
    from app.agent.prompts import SYSTEM_INSTRUCTIONS

    assert "A proposal changes nothing until you apply it" in SYSTEM_INSTRUCTIONS
    # Asserted on fragments rather than whole sentences: the prompt is wrapped.
    assert "call `apply_trip_patch` with that proposal_id" in SYSTEM_INSTRUCTIONS
    assert "never describe an itinerary" in SYSTEM_INSTRUCTIONS


@pytest.mark.parametrize(
    "script", ["scripts/plan_for_the_group.py", "scripts/choose_hotel_area.py"]
)
def test_the_scripts_survive_being_piped(script):
    """Japanese place names + a cp1252 pipe killed a script on its own output."""
    from pathlib import Path

    source = Path(script).read_text(encoding="utf-8")
    assert 'sys.stdout.reconfigure(encoding="utf-8"' in source
