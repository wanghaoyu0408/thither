"""Milestone 11 acceptance: the stress test / 旅行预演.

One test per criterion, through the real endpoints wherever a criterion is
about behaviour a traveller reaches. The engine's arithmetic itself is pinned
in tests/unit/test_simulation_service.py; what these hold up is the whole
route from a button to an honest answer.
"""

from datetime import timedelta

import pytest

import app.api.actions as actions_api
from app.db.repository import (
    CalibrationRepository,
    LearningRepository,
    TripRepository,
)
from app.models.lock import LockRecord
from app.services.validation_service import TravelLookup
from tests.unit.test_simulation_service import (
    DAY,
    at,
    measured,
    rainy_forecast,
    stop,
    trip,
)


@pytest.fixture
def measured_routes(monkeypatch):
    """Give the endpoint's route measurement a canned answer.

    The offline suite has no Google key, so `_measure` degrades to an empty
    lookup - correct, and useless for criteria about measured legs. The
    endpoint's only network seam is patched; everything after it is real.
    """

    def install(lookup: TravelLookup):
        async def fake_measure(_state, _days):
            return lookup

        monkeypatch.setattr(actions_api, "_measure", fake_measure)

    install(TravelLookup())
    return install


async def run(client, trip_id, body=None):
    response = await client.post(f"/trips/{trip_id}/stress-test", json=body or {})
    assert response.status_code == 200, response.text
    return response.json()


def the_day(forecast):
    assert len(forecast["days"]) == 1
    return forecast["days"][0]


# --- 1: an unmeasured leg never contributes zero ------------------------------


async def test_an_unmeasured_leg_never_contributes_zero(client, session, measured_routes):
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe", reservation_booked=True),
    )
    stored = await TripRepository(session).create(state)

    day = the_day(await run(client, stored.trip_id))

    stops = day["stops"]
    assert stops[-1]["rests_on_unknown"] is True
    assert [e["provenance"] for e in stops[-1]["inputs"]] == ["unknown"]
    kinds = [f["kind"] for f in day["findings"]]
    assert "unknown_dependency" in kinds
    # No lateness was manufactured out of an invented zero-minute journey.
    assert "late_arrival_risk" not in kinds and "tight_buffer" not in kinds
    assert day["verdict"] == "workable"


# --- 2: safe in expected, fragile in conservative -----------------------------


async def test_a_tight_reservation_turns_fragile_only_in_conservative(
    client, session, measured_routes
):
    state = trip(
        stop("Coffee", at(9), at(10, 30), "ent_museum"),
        stop(
            "Lunch reservation",
            at(11),
            at(12),
            "ent_cafe",
            time_flexibility="fixed",
            reservation_booked=True,
        ),
    )
    stored = await TripRepository(session).create(state)
    measured_routes(measured(("ent_museum", "ent_cafe", 28.0)))

    day = the_day(await run(client, stored.trip_id))

    lunch = next(s for s in day["stops"] if "Lunch" in s["title"])
    # Expected makes it; conservative may not; the verdict is that asymmetry.
    assert lunch["arrival"]["expected"]["high"] <= at(11).isoformat()
    assert lunch["arrival"]["conservative"]["high"] > at(11).isoformat()
    assert any(f["kind"] == "late_arrival_risk" and f["breaks"] for f in day["findings"])
    assert day["verdict"] == "fragile"


# --- 3: unknown parking is uncertainty, not "no parking" ----------------------


async def test_unknown_parking_widens_the_window_instead_of_denying_parking(
    client, session, measured_routes
):
    state = trip(
        stop("Trailhead", at(9), at(10), "ent_far_a"),
        stop("Lookout", at(11), at(12), "ent_far_b"),
        driving=True,
        entities={"ent_far_a": ["park"], "ent_far_b": ["park"]},
    )
    stored = await TripRepository(session).create(state)
    measured_routes(measured(("ent_far_a", "ent_far_b", 25.0), mode="driving"))

    day = the_day(await run(client, stored.trip_id))

    finding = next(f for f in day["findings"] if f["kind"] == "parking_uncertainty")
    assert "unverified" in finding["message"]
    assert finding["breaks"] is False, "uncertainty is not unavailability"
    lookout = next(s for s in day["stops"] if s["title"] == "Lookout")
    parking = next(e for e in lookout["inputs"] if e["what"] == "parking")
    assert parking["provenance"] == "assumption"
    assert (parking["low"], parking["high"]) == (5.0, 15.0)


# --- 4: weather makes a feasible outdoor stop fragile -------------------------


async def test_forecast_rain_makes_an_otherwise_feasible_outdoor_day_fragile(
    client, session, measured_routes
):
    state = trip(
        stop("Garden", at(14), at(16), "ent_far_a"),
        stop("Dinner", at(19), at(21), "ent_cafe", type="restaurant"),
        entities={"ent_far_a": ["garden"]},
    )
    stored_dry = await TripRepository(session).create(state)
    measured_routes(measured(("ent_far_a", "ent_cafe", 15.0), mode="transit"))
    dry = the_day(await run(client, stored_dry.trip_id))
    assert dry["verdict"] in ("comfortable", "workable")

    state.trip_id = "trip_wet_variant"
    state.itinerary.days[0].weather = rainy_forecast()
    stored_wet = await TripRepository(session).create(state)
    wet = the_day(await run(client, stored_wet.trip_id))

    weather = next(f for f in wet["findings"] if f["kind"] == "weather_exposure")
    assert weather["breaks"] is True
    assert wet["verdict"] == "fragile"


# --- 5 and 6: "Make this day safer" is the existing scoped replan -------------


def overloaded_two_day_state():
    state = trip(
        stop("One", at(9), at(9, 45), "ent_museum"),
        stop("Two", at(10), at(10, 45), "ent_cafe"),
        stop("Three", at(11), at(11, 45), "ent_museum"),
        stop("Four", at(12), at(12, 45), "ent_cafe"),
        stop("Five", at(13), at(13, 45), "ent_museum"),
        stop("Dinner", at(19), at(20), "ent_cafe", type="restaurant"),
    )
    # A second day that must come through byte-identical.
    second = state.itinerary.days[0].model_copy(deep=True)
    second.date = DAY + timedelta(days=1)
    second.items = [
        stop("Quiet museum", at(11) + timedelta(days=1), at(13) + timedelta(days=1), "ent_museum"),
    ]
    state.itinerary.days.append(second)
    state.brief.dates.end = second.date
    return state


async def test_the_safer_button_cannot_move_a_locked_item(client, session):
    state = overloaded_two_day_state()
    dinner = state.itinerary.days[0].items[-1]
    state.locks.append(
        LockRecord(
            target_kind="itinerary_item",
            target_id=dinner.item_id,
            reason="the booking is made",
        )
    )
    stored = await TripRepository(session).create(state)
    before = dinner.model_dump(mode="json")

    response = await client.post(
        f"/trips/{stored.trip_id}/days/{DAY.isoformat()}/replan",
        json={"intensity": "relaxed"},
    )
    assert response.status_code == 200

    after_state = await TripRepository(session).get(stored.trip_id)
    survivors = [
        item
        for day in after_state.itinerary.days
        for item in day.items
        if item.item_id == dinner.item_id
    ]
    assert len(survivors) == 1
    assert survivors[0].model_dump(mode="json") == before, "locked means byte-for-byte"


async def test_the_safer_replan_leaves_unrelated_days_byte_identical(client, session):
    state = overloaded_two_day_state()
    stored = await TripRepository(session).create(state)
    before = {
        day.date.isoformat(): day.model_dump(mode="json") for day in stored.itinerary.days
    }

    response = await client.post(
        f"/trips/{stored.trip_id}/days/{DAY.isoformat()}/replan",
        json={"intensity": "relaxed"},
    )
    body = response.json()
    assert body["applied"] is True, body.get("errors")

    after_state = await TripRepository(session).get(stored.trip_id)
    after = {
        day.date.isoformat(): day.model_dump(mode="json") for day in after_state.itinerary.days
    }
    assert set(before) == set(after)
    other = (DAY + timedelta(days=1)).isoformat()
    assert after[other] == before[other], "the untouched day must not move a byte"
    assert after[DAY.isoformat()] != before[DAY.isoformat()], "the touched day did change"
    # And the whole change was one atomic commit: exactly one revision spent.
    assert after_state.revision == stored.revision + 1


# --- 7: provenance is distinguishable all the way out -------------------------


async def test_every_input_names_what_kind_of_number_it_is(client, session, measured_routes):
    state = trip(
        stop("Trailhead", at(9), at(10), "ent_far_a"),
        stop("Lookout", at(11), at(12), "ent_far_b"),
        driving=True,
        entities={"ent_far_a": ["park"], "ent_far_b": ["park"]},
    )
    stored = await TripRepository(session).create(state)
    measured_routes(measured(("ent_far_a", "ent_far_b", 25.0), mode="driving"))

    day = the_day(await run(client, stored.trip_id))

    provenances = {
        e["provenance"] for s in day["stops"] for e in s["inputs"]
    }
    assert "measured" in provenances and "assumption" in provenances
    for s in day["stops"]:
        for estimate in s["inputs"]:
            assert estimate["provenance"] in {"measured", "calibrated", "assumption", "unknown"}
            if estimate["provenance"] == "unknown":
                assert estimate["low"] is None and estimate["high"] is None


# --- 8: the model explains; the engine computes -------------------------------


def test_the_prompt_forbids_model_schedule_arithmetic():
    from app.agent.prompts import SYSTEM_INSTRUCTIONS

    assert "you never do schedule arithmetic yourself" in SYSTEM_INSTRUCTIONS
    assert "run_stress_test" in SYSTEM_INSTRUCTIONS
    # And the tool's own return says the same thing at the moment of use.
    from app.agent.tool_registry import TOOL_SCHEMAS

    schema = next(s for s in TOOL_SCHEMAS if s["name"] == "run_stress_test")
    assert "never compute or adjust a schedule figure yourself" in schema["description"]


async def test_the_stress_tool_reads_and_never_writes(session):
    from app.agent.tool_registry import ToolContext, _run_stress_test
    from app.services.proposal_store import ProposalStore
    from app.config import Settings

    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
    )
    snapshot = state.model_dump(mode="json")
    context = ToolContext(
        state=state, toolbox=None, proposals=ProposalStore(), settings=Settings()
    )

    report = await _run_stress_test(context, {})

    assert report["worst"] in {"comfortable", "workable", "fragile", "blocking"}
    assert "never redo or adjust the arithmetic" in report["note"]
    assert state.model_dump(mode="json") == snapshot


# --- 9: the preview writes nothing at all -------------------------------------


async def test_a_stress_test_spends_no_revision_and_no_rows(client, session, measured_routes):
    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
    )
    stored = await TripRepository(session).create(state)
    outcomes_before = len(await CalibrationRepository(session).list_for())

    await run(client, stored.trip_id)
    await run(client, stored.trip_id)  # twice, for luck and idempotence

    after = await TripRepository(session).get(stored.trip_id)
    assert after.revision == stored.revision
    assert after.model_dump(mode="json") == stored.model_dump(mode="json")
    assert len(await CalibrationRepository(session).list_for()) == outcomes_before
    assert await LearningRepository(session).list_for_profile("user_anybody") == []


# --- 10: four words, no fake precision ----------------------------------------


async def test_the_response_carries_verdict_words_and_no_scores(
    client, session, measured_routes
):
    import json as json_module

    state = trip(
        stop("Museum", at(14), at(16), "ent_museum"),
        stop("Dinner", at(19), at(21), "ent_cafe"),
    )
    stored = await TripRepository(session).create(state)

    body = await run(client, stored.trip_id)

    assert body["worst"] in {"comfortable", "workable", "fragile", "blocking"}
    for day in body["days"]:
        assert day["verdict"] in {"comfortable", "workable", "fragile", "blocking"}
    flat = json_module.dumps(body)
    for banned in ("feasibility_score", "confidence_percent", "% feasible", "probability_of"):
        assert banned not in flat


# --- 11: folds carry the validator's words ------------------------------------
# (Pinned at the unit level in test_folded_warnings_carry_the_validators_words_
# not_new_numbers - the fold map and message equality live there, where the
# issue can be injected precisely.)


# --- 12: a single day can be previewed ----------------------------------------


async def test_preview_one_day_returns_that_day_alone(client, session, measured_routes):
    state = overloaded_two_day_state()
    stored = await TripRepository(session).create(state)

    body = await run(client, stored.trip_id, {"day": DAY.isoformat()})

    assert [d["date"] for d in body["days"]] == [DAY.isoformat()]

    missing = await client.post(
        f"/trips/{stored.trip_id}/stress-test", json={"day": "2031-01-01"}
    )
    assert missing.status_code == 404
