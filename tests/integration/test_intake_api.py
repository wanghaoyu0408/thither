"""Answering, confirming, reopening - and what confirmation has to actually mean.

Confirmation is the point of the feature, so the assertions are about what is
*persisted*: a status, a snapshot, the revision it was agreed at, and a time.
A confirmation that only existed in the conversation would satisfy none of them.
"""

from datetime import date

from app.db.repository import TripRepository
from app.models.intake import ClarificationChoice, ClarificationQuestion
from app.models.trip import TripState
from app.services.intake_service import research_allowed


def ready_trip() -> TripState:
    """Everything blocking is known; nothing has been confirmed."""
    state = TripState.new(title="Maui")
    state.brief.destination.city = "Maui"
    state.brief.destination.flexible = False
    state.brief.dates.start = date(2026, 8, 10)
    state.brief.dates.end = date(2026, 8, 14)
    state.brief.party.adults = 2
    state.brief.priorities = ["food", "beaches"]
    state.brief.scope.flights = "already_arranged"
    state.brief.scope.lodging = "already_arranged"
    state.brief.scope.rental_car = "already_arranged"
    state.intake.status = "awaiting_confirmation"
    return state


def unready_trip() -> TripState:
    state = TripState.new(title="Maui")
    state.brief.destination.city = "Maui"
    state.intake.questions.append(
        ClarificationQuestion(
            question_id="clq_when",
            question="When are you going?",
            kind="single_choice",
            fills="/brief/dates",
            choices=[
                ClarificationChoice(value="aug", label="Mid August"),
                ClarificationChoice(value="sep", label="September"),
            ],
        )
    )
    return state


# --- reading -----------------------------------------------------------------


async def test_the_view_shows_what_will_and_will_not_be_searched(client, session):
    trip = await TripRepository(session).create(ready_trip())

    body = (await client.get(f"/trips/{trip.trip_id}/intake")).json()

    assert body["status"] == "awaiting_confirmation"
    assert body["already_arranged"] == ["Flights", "Hotel", "Rental car"]
    assert body["will_not_search"] == ["Flights", "Hotel"]
    assert body["ready_to_confirm"] is True
    assert body["research_allowed"] is False, "not until they say so"


async def test_an_unknown_party_is_reported_as_unknown_not_as_one(client, session):
    state = ready_trip()
    state.brief.party.adults = None
    trip = await TripRepository(session).create(state)

    body = (await client.get(f"/trips/{trip.trip_id}/intake")).json()

    assert body["travelers"] is None, "a count nobody gave must not be shown as a number"


# --- answering ---------------------------------------------------------------


async def test_answering_records_both_the_choice_and_the_words(client, session):
    trip = await TripRepository(session).create(unready_trip())

    result = (
        await client.post(
            f"/trips/{trip.trip_id}/intake/answers",
            json={
                "answers": [
                    {
                        "question_id": "clq_when",
                        "values": ["aug"],
                        "text": "second week if we can get the flights",
                    }
                ]
            },
        )
    ).json()

    assert result["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    question = persisted.intake.questions[0]
    assert question.answered is True
    assert question.values == ["aug"]
    # A structured question must never trap someone whose answer is not on the
    # list, so the words are kept alongside the choice.
    assert question.answer == "second week if we can get the flights"
    assert persisted.intake.unanswered == []


async def test_a_free_text_answer_to_a_choice_question_is_accepted(client, session):
    trip = await TripRepository(session).create(unready_trip())

    result = (
        await client.post(
            f"/trips/{trip.trip_id}/intake/answers",
            json={"answers": [{"question_id": "clq_when", "text": "actually next spring"}]},
        )
    ).json()

    assert result["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.intake.questions[0].answer == "actually next spring"
    assert persisted.intake.questions[0].values == []


async def test_an_empty_answer_changes_nothing(client, session):
    trip = await TripRepository(session).create(unready_trip())

    result = (
        await client.post(
            f"/trips/{trip.trip_id}/intake/answers",
            json={"answers": [{"question_id": "clq_when", "text": "  "}]},
        )
    ).json()

    assert result["applied"] is False
    assert (await TripRepository(session).get(trip.trip_id)).revision == trip.revision


async def test_answering_an_unknown_question_is_a_404(client, session):
    trip = await TripRepository(session).create(unready_trip())

    response = await client.post(
        f"/trips/{trip.trip_id}/intake/answers",
        json={"answers": [{"question_id": "clq_nope", "text": "x"}]},
    )

    assert response.status_code == 404


# --- confirming ---------------------------------------------------------------


async def test_confirmation_is_a_persisted_transition(client, session):
    trip = await TripRepository(session).create(ready_trip())
    before = trip.revision

    result = (await client.post(f"/trips/{trip.trip_id}/intake/confirm")).json()

    assert result["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.intake.status == "confirmed"
    assert persisted.intake.confirmed_at is not None
    assert persisted.intake.confirmed_revision == before
    # The snapshot is what was agreed, kept apart from the live brief so later
    # edits cannot quietly rewrite the record.
    assert persisted.intake.confirmed_brief is not None
    assert persisted.intake.confirmed_brief.destination.city == "Maui"
    assert research_allowed(persisted) is True


async def test_confirmation_refuses_while_something_blocking_is_missing(client, session):
    trip = await TripRepository(session).create(unready_trip())

    response = await client.post(f"/trips/{trip.trip_id}/intake/confirm")

    assert response.status_code == 409
    assert "nothing can be scheduled" in response.json()["detail"]
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.intake.status == "collecting"
    assert research_allowed(persisted) is False


async def test_confirming_twice_spends_no_revision(client, session):
    trip = await TripRepository(session).create(ready_trip())
    first = (await client.post(f"/trips/{trip.trip_id}/intake/confirm")).json()

    second = (await client.post(f"/trips/{trip.trip_id}/intake/confirm")).json()

    assert second["applied"] is False
    assert second["revision"] == first["revision"]


# --- editing after confirmation ------------------------------------------------


async def test_editing_a_fact_after_confirmation_does_not_reopen_intake(client, session):
    """Changing the traveller count is an edit, not a new trip. The snapshot
    keeps the original so the difference stays visible."""
    trip = await TripRepository(session).create(ready_trip())
    await client.post(f"/trips/{trip.trip_id}/intake/confirm")
    current = await TripRepository(session).get(trip.trip_id)

    patched = await client.post(
        f"/trips/{trip.trip_id}/patch",
        json={
            "base_revision": current.revision,
            "reason": "there are three of us now",
            "actor": "user",
            "operations": [{"op": "set", "path": "/brief/party/adults", "value": 3}],
        },
    )

    assert patched.json()["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.brief.party.adults == 3
    assert persisted.intake.status == "confirmed"
    assert persisted.intake.confirmed_brief.party.adults == 2, "the snapshot is a record"


async def test_reopening_needs_a_reason_and_holds_research_again(client, session):
    trip = await TripRepository(session).create(ready_trip())
    await client.post(f"/trips/{trip.trip_id}/intake/confirm")

    result = (
        await client.post(
            f"/trips/{trip.trip_id}/intake/reopen",
            json={"reason": "they changed destination entirely"},
        )
    ).json()

    assert result["applied"] is True
    persisted = await TripRepository(session).get(trip.trip_id)
    assert persisted.intake.status == "collecting"
    assert research_allowed(persisted) is False
