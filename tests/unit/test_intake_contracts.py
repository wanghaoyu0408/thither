"""The four contracts intake rests on, pinned against the ways they slipped.

Each of these covers a failure that a live run produced and that no amount of
prompt wording fixed: a date whose year the model would not commit to, a reply
that claimed to have saved what it had not, a question about something nobody
was waiting on, and "United States" recorded as the destination of a Maui trip.
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agent import context as agent_context
from app.agent.context import summarize
from app.agent.prompts import build_instructions
from app.agent.tool_registry import (
    ToolContext,
    _ask_clarifications,
    _update_trip_brief,
)
from app.config import Settings
from app.db.repository import TripRepository
from app.models.intake import ClarificationQuestion
from app.models.trip import TripState
from app.services import intake_service
from app.services.intake_service import missing_blocking, resolve_date, today_at
from app.services.proposal_store import ProposalStore


def trip(*, timezone=None, destination_city=None, ready=False) -> TripState:
    state = TripState.new(title="t")
    state.brief.timezone = timezone
    state.brief.destination.city = destination_city
    if ready:
        # Everything blocking known, so intake is only waiting to be confirmed.
        state.brief.destination.city = destination_city or "Maui"
        state.brief.dates.start = date(2026, 8, 10)
        state.brief.dates.end = date(2026, 8, 14)
        state.brief.scope.flights = "already_arranged"
        state.brief.scope.lodging = "already_arranged"
    return state


def context_for(state: TripState) -> ToolContext:
    return ToolContext(
        state=state, toolbox=None, proposals=ProposalStore(), settings=Settings()
    )


def brief_after(context: ToolContext, path: str):
    for operation in context.pending_brief_ops:
        if operation["path"] == path:
            return operation["value"]
    return None


# --- 1. the year comes from the clock, not from the prompt -------------------


@pytest.mark.parametrize(
    ("today", "given", "expected"),
    [
        # Said in August about a date two days away: this year.
        (date(2026, 8, 9), "8/10", date(2026, 8, 10)),
        # Said in December about a date in August: next year.
        (date(2026, 12, 20), "8/10", date(2027, 8, 10)),
        # Today itself counts as the next occurrence, not last year's.
        (date(2026, 8, 10), "8/10", date(2026, 8, 10)),
        # Yesterday rolls forward rather than landing in the past.
        (date(2026, 8, 11), "8/10", date(2027, 8, 10)),
        # A full ISO date is taken as given, whatever the clock says.
        (date(2030, 1, 1), "2026-08-10", date(2026, 8, 10)),
    ],
)
def test_a_year_less_date_resolves_against_the_current_date(today, given, expected):
    assert resolve_date(given, today) == expected


def test_an_unparseable_date_is_not_guessed_at():
    assert resolve_date("sometime soon", date(2026, 8, 9)) is None
    assert resolve_date("13/45", date(2026, 8, 9)) is None


def test_today_follows_the_destination_timezone(monkeypatch):
    """Late evening in California is already tomorrow in Tokyo, and a trip to
    Tokyo should resolve its dates the way the destination reads them."""
    evening = datetime(2026, 8, 9, 23, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return evening.astimezone(tz)

    monkeypatch.setattr(intake_service, "datetime", FixedDatetime)

    assert today_at(trip(timezone="Asia/Tokyo")) == date(2026, 8, 10)
    assert today_at(trip(timezone="America/Los_Angeles")) == date(2026, 8, 9)
    # No timezone on the trip falls back to UTC rather than inventing one.
    assert today_at(trip()) == intake_service.utcnow().date()


def test_an_unknown_timezone_falls_back_rather_than_raising():
    assert today_at(trip(timezone="Mars/Olympus_Mons")) == intake_service.utcnow().date()


def test_the_date_is_injected_at_runtime_and_never_written_into_the_prompt():
    """A date in the prompt is frozen at whatever day the prompt was edited."""
    instructions = build_instructions()

    assert "today" in instructions, "the prompt should point at the state, not carry a date"
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", instructions), (
        "the system prompt must not contain a hard-coded date"
    )

    first = summarize(trip())
    assert first["today"] == agent_context.today_at(trip()).isoformat()
    assert first["date_zone"] == "UTC"
    assert summarize(trip(timezone="Pacific/Honolulu"))["date_zone"] == "Pacific/Honolulu"


async def test_update_trip_brief_stores_a_resolved_date():
    state = trip()
    context = context_for(state)
    today = today_at(state)
    tomorrow = today + timedelta(days=1)

    await _update_trip_brief(
        context, {"start_date": f"{tomorrow.month}/{tomorrow.day}", "destination_city": "Maui"}
    )

    assert brief_after(context, "/brief/dates/start") == tomorrow.isoformat()


# --- 2. success is only reported after a committed revision ------------------


async def test_an_intake_write_reports_the_revision_it_actually_committed(client, session):
    repo = TripRepository(session)
    stored = await repo.create(trip(ready=True))

    result = (await client.post(f"/trips/{stored.trip_id}/intake/confirm")).json()

    persisted = await repo.get(stored.trip_id)
    assert result["applied"] is True
    assert result["revision"] == persisted.revision, "reported a revision the store does not hold"
    assert result["trip"]["revision"] == persisted.revision
    assert result["trip"]["intake"]["status"] == "confirmed"


async def test_a_refused_intake_write_reports_no_success_and_no_revision(
    client, session, monkeypatch
):
    """If the commit does not happen, nothing may say it did - and the caller
    gets the stored state back so the screen never runs ahead of the store."""
    repo = TripRepository(session)
    stored = await repo.create(trip(ready=True))
    before = stored.revision

    from app.models.patch import PatchError, PatchResult

    async def refuse(self, trip_id, patches):
        return [
            PatchResult(
                applied=False,
                revision=before,
                errors=[PatchError(code="REVISION_CONFLICT", message="someone else got there")],
            )
        ]

    monkeypatch.setattr(TripRepository, "apply_patches", refuse)

    result = (await client.post(f"/trips/{stored.trip_id}/intake/confirm")).json()

    assert result["applied"] is False
    assert result["revision"] == before
    assert [error["code"] for error in result["errors"]] == ["REVISION_CONFLICT"]
    assert result["trip"]["intake"]["status"] != "confirmed"


# --- 3. a question must name an outstanding requirement ----------------------


async def test_every_gap_carries_a_stable_requirement_id():
    ids = {gap.requirement_id for gap in missing_blocking(trip())}

    assert ids == {"dates", "scope.flights", "scope.lodging"}


async def test_a_question_without_a_known_requirement_id_is_not_asked():
    context = context_for(trip())

    reply = await _ask_clarifications(
        context,
        {
            "questions": [
                {"question": "What is your budget?", "kind": "text", "requirement_id": "budget"}
            ]
        },
    )

    assert "__patches__" not in reply
    assert "outstanding requirement_id" in reply["error"]
    # And it says what it *is* waiting on, so the next attempt can be right.
    assert {row["requirement_id"] for row in reply["outstanding"]} == {
        "dates",
        "scope.flights",
        "scope.lodging",
    }


async def test_a_question_naming_a_real_requirement_carries_it_through():
    context = context_for(trip())

    reply = await _ask_clarifications(
        context,
        {
            "questions": [
                {"question": "Shall I look for flights?", "kind": "single_choice",
                 "requirement_id": "scope.flights"}
            ]
        },
    )

    stored = reply["__patches__"][0]["operations"][0]["value"]
    assert stored["requirement_id"] == "scope.flights"
    # The pointer is filled in from the gap rather than taken on trust.
    assert stored["fills"] == "/brief/scope/flights"


# --- 4. a country is not a destination ---------------------------------------


async def test_maui_is_recorded_as_a_region_not_a_country():
    context = context_for(trip())

    await _update_trip_brief(context, {"destination_region": "Maui", "destination_country": "USA"})

    assert brief_after(context, "/brief/destination/region") == "Maui"
    assert brief_after(context, "/brief/destination/country") == "USA"


async def test_a_country_on_its_own_is_refused():
    """"United States" was recorded as the destination of a Maui trip, leaving
    the hotel and area services - both of which read destination.city - nothing
    to work with."""
    context = context_for(trip())

    reply = await _update_trip_brief(context, {"destination_country": "United States"})

    assert "not a destination" in reply["error"]
    assert context.pending_brief_ops == [], "nothing may be written on the way to refusing"


async def test_a_country_is_allowed_beside_a_place_already_known():
    context = context_for(trip(destination_city="Maui"))

    await _update_trip_brief(context, {"destination_country": "United States"})

    assert brief_after(context, "/brief/destination/country") == "United States"


# --- 5. a spent question does not hold the trip shut --------------------------


def one_gap_left() -> TripState:
    """A trip waiting on lodging scope, and a question asking for it."""
    state = trip(destination_city="Los Angeles")
    state.brief.dates.start = date(2026, 9, 1)
    state.brief.dates.end = date(2026, 9, 3)
    state.brief.scope.flights = "not_needed"
    state.intake.questions.append(
        ClarificationQuestion(
            question="住宿需要我帮你找酒店，还是你们已经安排好了？",
            kind="single_choice",
            requirement_id="scope.lodging",
            fills="/brief/scope/lodging",
        )
    )
    return state


async def test_a_question_answered_in_the_chat_stops_holding_the_brief_shut():
    """The bug this whole change exists for.

    The traveller said "找酒店" in the conversation, the agent recorded the
    scope, and the gap closed - but the question stayed unanswered forever,
    keeping `ready_to_confirm` false and the confirm button out of the DOM. The
    trip could not be confirmed and so could never be researched.
    """
    state = one_gap_left()
    question = state.intake.questions[0]
    assert intake_service.outstanding_questions(state) == [question]
    assert intake_service.ready_to_confirm(state) is False

    context = context_for(state)
    reply = await _update_trip_brief(context, {"scope": {"lodging": "plan"}})

    # The tool flips the trip to awaiting_confirmation, which it decides with
    # the same readiness test the endpoint and the button use. Before the fix
    # the outstanding question vetoed this and nothing ever moved.
    operations = reply["__patches__"][0]["operations"]
    assert {
        "op": "set",
        "path": "/intake/status",
        "value": "awaiting_confirmation",
    } in operations

    state.brief.scope.lodging = "plan"
    assert intake_service.outstanding_questions(state) == []
    assert intake_service.ready_to_confirm(state) is True
    # Spent, not answered. Nobody typed anything into it, and writing an
    # `answered_at` would claim they had.
    assert question.answered is False
    assert question in state.intake.unanswered


@pytest.mark.parametrize("lodging", ["unknown", "plan"])
async def test_ready_to_confirm_predicts_what_confirm_actually_does(client, session, lodging):
    """A readiness flag that does not predict the thing it gates is worse than
    no flag: it used to be stricter than the endpoint, so the browser disabled a
    button the server would have honoured."""
    state = one_gap_left()
    state.brief.scope.lodging = lodging
    expected = intake_service.ready_to_confirm(state)

    stored = await TripRepository(session).create(state)
    response = await client.post(f"/trips/{stored.trip_id}/intake/confirm")

    assert (response.status_code == 200) is expected
    assert (response.json().get("trip", {}).get("intake", {}).get("status") == "confirmed") is (
        expected
    )


def test_the_origin_reaches_the_model():
    """A traveller typed Cupertino into the very first form; the agent asked
    for the departure city again in chat. The origin was stored all along -
    `summarize()` simply did not carry it, so the model could not see it."""
    state = trip(destination_city="Kyoto")
    state.brief.origin.city = "Cupertino"
    state.brief.origin.airport_codes = ["SFO"]

    brief = summarize(state)["brief"]

    assert brief["origin"] == {"city": "Cupertino", "airport_codes": ["SFO"]}


def test_other_requirements_reach_the_model():
    """The same defect as the origin, in a field nobody had ever read.

    `brief.notes` was writable from the first milestone - the tool declared it,
    `_brief_ops` mapped it - and `summarize()` never carried it, so anything a
    traveller asked to be planned around went into the store and stopped there.
    """
    state = trip(destination_city="Kyoto")
    state.brief.notes = "back by 9pm on the 23rd, and no long walks"

    brief = summarize(state)["brief"]

    assert brief["notes"] == "back by 9pm on the 23rd, and no long walks"


async def test_the_model_is_not_shown_a_question_it_no_longer_needs_to_ask():
    """`questions_awaiting_answer` is what stops the agent re-asking. A spent
    question left in there is an instruction to ask it again, every turn."""
    state = one_gap_left()
    assert summarize(state)["intake"]["questions_awaiting_answer"] == [
        state.intake.questions[0].question
    ]

    state.brief.scope.lodging = "plan"
    assert summarize(state)["intake"]["questions_awaiting_answer"] == []
