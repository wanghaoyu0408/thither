"""A fork the agent reached, and the traveller answering it.

The reply that prompted all of this said the right thing and left nothing to
press: no airline flew ALB to MDW on those dates, and the practical next step
was O'Hare. These pin the mechanism that turns that sentence into a button, and
the honest way to park a part of the trip when the answer is "not now".
"""



from app.config import Settings
from app.db.repository import TripRepository
from app.models.decision import Decision, DecisionOption, FlightOptionData
from app.models.flight import AirportOption
from app.models.proposal import AgentProposal, ProposalChoice
from app.models.trip import TripState
from app.services.intake_service import should_shop_for
from app.services.next_step import next_steps
from app.services.proposal_store import ProposalStore
from tests.conftest import sample_state


def airport(iata: str, name: str) -> AirportOption:
    return AirportOption(iata=iata, name=name, city="Chicago", lat=41.9, lng=-87.8)


def stuck_trip() -> TripState:
    """Arrival airport settled on MDW; a flight search that found nothing."""
    state = sample_state()
    state.intake.status = "confirmed"
    state.brief.origin.city = "Albany"
    state.brief.scope.flights = "plan"
    state.brief.scope.lodging = "plan"
    state.decisions.arrival_airport = Decision[AirportOption](
        decision_id="dec_arr",
        status="selected",
        options=[
            DecisionOption[AirportOption](
                option_id="opt_mdw", data=airport("MDW", "Chicago Midway International Airport"),
                status="selected",
            ),
            DecisionOption[AirportOption](
                option_id="opt_ord", data=airport("ORD", "Chicago O'Hare International Airport"),
                status="shortlisted",
            ),
        ],
        selected_option_id="opt_mdw",
    )
    # What the empty search now leaves behind.
    state.decisions.flights = Decision[FlightOptionData](
        decision_id="dec_flights",
        status="researching",
        rationale="searched ALB to MDW on 2026-10-01; no airline offered it",
        options=[],
    )
    return state


def fork() -> AgentProposal:
    return AgentProposal(
        proposal_id="prp_ord",
        question="Fly into O'Hare instead?",
        detail="No airline offered ALB to MDW on those dates.",
        choices=[
            ProposalChoice(label="Fly into ORD instead", action="select_option",
                           decision="arrival_airport", option_id="opt_ord",
                           note="a longer drive, but airlines actually fly it"),
            ProposalChoice(label="Leave flights for now", action="set_aside", part="flights",
                           note="flights stay unplanned until you say otherwise"),
        ],
    )


async def stored(session, state: TripState) -> TripState:
    return await TripRepository(session).create(state)


def tool_context(state: TripState):
    from app.agent.tool_registry import ToolContext

    return ToolContext(state=state, toolbox=None, proposals=ProposalStore(), settings=Settings())


# --- proposing ----------------------------------------------------------------


async def test_a_proposal_names_choices_that_exist():
    """A button that cannot be pressed is the failure this tool exists to end."""
    from app.agent.tool_registry import _propose_next_step

    context = tool_context(stuck_trip())

    result = await _propose_next_step(context, {
        "question": "Fly into O'Hare instead?",
        "choices": [
            {"label": "Fly into ORD", "action": "select_option",
             "decision": "arrival_airport", "option_id": "opt_nonexistent"},
            {"label": "Leave it", "action": "set_aside", "part": "flights"},
        ],
    })

    assert "error" in result
    assert "opt_nonexistent" in result["error"]
    # And it says what it could have offered instead.
    assert "opt_ord" in result["options"]


async def test_a_proposal_needs_a_way_out():
    from app.agent.tool_registry import _propose_next_step

    context = tool_context(stuck_trip())

    result = await _propose_next_step(context, {
        "question": "Fly into O'Hare instead?",
        "choices": [{"label": "Yes", "action": "select_option",
                     "decision": "arrival_airport", "option_id": "opt_ord"}],
    })

    assert "at least two choices" in result["error"]


async def test_a_good_proposal_commits_itself():
    from app.agent.tool_registry import _propose_next_step

    context = tool_context(stuck_trip())

    result = await _propose_next_step(context, {
        "question": "Fly into O'Hare instead?",
        "detail": "No airline offered ALB to MDW on those dates.",
        "choices": [
            {"label": "Fly into ORD instead", "action": "select_option",
             "decision": "arrival_airport", "option_id": "opt_ord"},
            {"label": "Leave flights for now", "action": "set_aside", "part": "flights"},
        ],
    })

    ops = result["__patches__"][0]["operations"]
    added = [op for op in ops if op["path"] == "/proposals/-"]
    assert len(added) == 1
    assert added[0]["value"]["question"] == "Fly into O'Hare instead?"
    assert len(added[0]["value"]["choices"]) == 2


async def test_the_same_fork_is_not_asked_twice():
    from app.agent.tool_registry import _propose_next_step

    state = stuck_trip()
    state.proposals = [fork()]
    context = tool_context(state)

    result = await _propose_next_step(context, {
        "question": "Fly into O'Hare instead?",
        "choices": [
            {"label": "Fly into ORD instead", "action": "select_option",
             "decision": "arrival_airport", "option_id": "opt_ord"},
            {"label": "Leave flights for now", "action": "set_aside", "part": "flights"},
        ],
    })

    assert result["asked"] == 0
    assert "do not ask it twice" in result["note"]


# --- answering ----------------------------------------------------------------


async def test_answering_switches_the_decision_and_marks_it_answered(client, session):
    state = stuck_trip()
    state.proposals = [fork()]
    trip = await stored(session, state)

    response = await client.post(
        f"/trips/{trip.trip_id}/proposals/prp_ord/answer", json={"choice_index": 0}
    )
    assert response.status_code == 200 and response.json()["applied"] is True

    persisted = await TripRepository(session).get(trip.trip_id)
    # Both halves in one patch: the airport moved and the question closed.
    assert persisted.decisions.arrival_airport.selected_option_id == "opt_ord"
    assert persisted.proposals[0].answered is True
    assert persisted.proposals[0].chosen_label == "Fly into ORD instead"
    # And the option it replaced is a shortlisted option again, not a second
    # thing marked "selected".
    statuses = {o.option_id: o.status for o in persisted.decisions.arrival_airport.options}
    assert statuses == {"opt_ord": "selected", "opt_mdw": "shortlisted"}


async def test_answering_twice_spends_no_revision(client, session):
    state = stuck_trip()
    state.proposals = [fork()]
    trip = await stored(session, state)

    first = (await client.post(
        f"/trips/{trip.trip_id}/proposals/prp_ord/answer", json={"choice_index": 0}
    )).json()
    second = (await client.post(
        f"/trips/{trip.trip_id}/proposals/prp_ord/answer", json={"choice_index": 1}
    )).json()

    assert second["applied"] is False
    assert second["revision"] == first["revision"]
    assert "already answered" in second["summary"]


async def test_an_unknown_proposal_is_a_404(client, session):
    trip = await stored(session, stuck_trip())

    response = await client.post(
        f"/trips/{trip.trip_id}/proposals/prp_nope/answer", json={"choice_index": 0}
    )
    assert response.status_code == 404


# --- setting aside ------------------------------------------------------------


async def test_setting_a_part_aside_stops_it_being_a_next_step(client, session):
    """Parked, without claiming the flights are booked or unwanted.

    `not_needed` would say they are not flying; `already_arranged` would say
    tickets exist; `unknown` would re-open a blocking intake gap and un-confirm
    the brief. All three are lies, so the reason lives on the decision.
    """
    state = stuck_trip()
    state.proposals = [fork()]
    trip = await stored(session, state)

    assert any(s.what == "flights" for s in next_steps(trip))

    response = await client.post(
        f"/trips/{trip.trip_id}/proposals/prp_ord/answer", json={"choice_index": 1}
    )
    assert response.status_code == 200 and response.json()["applied"] is True

    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.decisions.flights.set_aside_reason
    assert should_shop_for(persisted, "flights") is False
    assert not any(s.what == "flights" for s in next_steps(persisted))

    # The traveller's standing instruction is untouched: they do still want
    # flights, we just could not find any.
    assert persisted.brief.scope.flights == "plan"
    # And the rest of the trip carries on.
    assert any(s.what in ("hotel_area", "places", "itinerary") for s in next_steps(persisted))


async def test_resuming_puts_it_back(client, session):
    state = stuck_trip()
    state.decisions.flights.set_aside_reason = "nothing flew that route"
    state.proposals = [
        AgentProposal(
            proposal_id="prp_resume",
            question="Look for flights again?",
            choices=[
                ProposalChoice(label="Yes, try again", action="resume", part="flights"),
                ProposalChoice(label="Still leave it", action="none"),
            ],
        )
    ]
    trip = await stored(session, state)
    assert should_shop_for(trip, "flights") is False

    await client.post(
        f"/trips/{trip.trip_id}/proposals/prp_resume/answer", json={"choice_index": 0}
    )

    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.decisions.flights.set_aside_reason is None
    assert should_shop_for(persisted, "flights") is True


async def test_carrying_on_changes_nothing_but_the_answer(client, session):
    state = stuck_trip()
    state.proposals = [
        AgentProposal(
            proposal_id="prp_none",
            question="Anything else before I carry on?",
            choices=[
                ProposalChoice(label="Carry on", action="none"),
                ProposalChoice(label="Leave flights for now", action="set_aside", part="flights"),
            ],
        )
    ]
    trip = await stored(session, state)

    await client.post(
        f"/trips/{trip.trip_id}/proposals/prp_none/answer", json={"choice_index": 0}
    )

    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.proposals[0].answered is True
    assert persisted.decisions.flights.set_aside_reason is None
    assert persisted.decisions.arrival_airport.selected_option_id == "opt_mdw"


# --- the empty search is a finding --------------------------------------------


def test_a_search_that_found_nothing_is_still_work():
    """A decision with no options is not a decision made.

    `next_steps` tested whether the decision existed, so the step vanished the
    moment a search came back empty - exactly when it matters most.
    """
    state = stuck_trip()

    steps = {(s.what, s.label) for s in next_steps(state)}
    assert ("flights", "Try other airports or dates for flights") in steps
