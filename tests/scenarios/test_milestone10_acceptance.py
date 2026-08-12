"""Milestone 10 acceptance: the agent measures its own error.

One test per criterion. The through-line is the same discipline the rest of
the codebase runs on, pointed at this system's own figures: a claim is only
worth something if something could contradict it, an interval is only honest
if it admits what it leaves out, and "we have never checked" is an answer that
has to be said rather than rendered as silence.
"""

from datetime import date, timedelta

import pytest

from app.config import Settings
from app.db.repository import CalibrationRepository, ProfileRepository, TripRepository
from app.models.common import Money
from app.models.decision import (
    Decision,
    DecisionOption,
    HotelAreaOption,
    HotelOptionData,
)
from app.models.hotel import HotelPriceQuote
from app.models.traveler import TravelerProfile
from app.models.trip import TripTraveler
from app.services.calibration_service import (
    automatic_outcomes,
    calibrate,
    calibration_for,
    outcome_for,
    outcome_from_measurement,
    predictions_from,
)
from tests.conftest import sample_state

SETTINGS = Settings(calibration_min_samples=5, calibration_confident_samples=12)
SCOPE = "Asia/Tokyo"


def kyoto(*, ended: bool = False, profile_id: str | None = None):
    state = sample_state()
    state.brief.timezone = SCOPE
    state.travelers = [
        TripTraveler(
            traveler_id="trv_1", name="Haoyu", role="organizer", profile_id=profile_id
        )
    ]
    if ended:
        # The itinerary days move with the brief, or the integrity check
        # rightly refuses the patch: a day after the trip ends is a broken
        # trip, whatever the reflection is trying to say about it.
        today = date.today()
        span = state.brief.dates.end - state.brief.dates.start
        was = state.brief.dates.start
        state.brief.dates.start = today - timedelta(days=span.days + 4)
        state.brief.dates.end = today - timedelta(days=4)
        shift = state.brief.dates.start - was
        for day in state.itinerary.days:
            day.date = day.date + shift
            for item in day.items:
                if item.start_at:
                    item.start_at = item.start_at + shift
                if item.end_at:
                    item.end_at = item.end_at + shift
    state.decisions.hotel_area = Decision(
        options=[
            DecisionOption(
                option_id="opt_gion",
                data=HotelAreaOption(area_name="Gion", mean_minutes=14.0, travel_mode="transit"),
            ),
            DecisionOption(
                option_id="opt_arashiyama",
                data=HotelAreaOption(
                    area_name="Arashiyama", mean_minutes=31.0, travel_mode="transit"
                ),
            ),
        ],
        selected_option_id="opt_gion",
    )
    state.decisions.hotel = Decision(
        options=[
            DecisionOption(
                option_id="opt_ryokan",
                data=HotelOptionData(
                    provider="serpapi_google_hotels",
                    live_mode=True,
                    name="Gion Ryokan",
                    entity_id="ent_ryokan",
                    headline_nightly=Money(amount=180.0),
                    quotes=[
                        HotelPriceQuote(source="a booking site", nightly=Money(amount=214.0))
                    ],
                ),
            )
        ],
        selected_option_id="opt_ryokan",
    )
    return state


def answers(n: int, answer: str = "a_bit_longer"):
    template = predictions_from(kyoto())[0]
    return [
        outcome_for(template.model_copy(update={"prediction_id": f"pred_{i}"}), answer=answer)
        for i in range(n)
    ]


def record(corpus, mode: str = "transit", scope: str = SCOPE):
    return calibration_for(
        corpus,
        provider="google_routes",
        dimension="travel_minutes",
        mode=mode,
        scope=scope,
        settings=SETTINGS,
    )


# --- 1 ------------------------------------------------------------------------


async def test_a_figure_is_a_prediction_and_no_row_was_written_to_say_so(session):
    """Acceptance 1. Every checkable claim is derived from the trip. Nothing
    was written at search time, which is also why trips planned before this
    milestone existed are covered."""
    stored = await TripRepository(session).create(kyoto())

    predictions = predictions_from(stored)

    assert {p.dimension for p in predictions} == {"travel_minutes", "hotel_headline_gap"}
    assert all(p.trip_id == stored.trip_id for p in predictions)
    # The only table this milestone added holds outcomes, and it is empty.
    assert await CalibrationRepository(session).list_for() == []


# --- 2 ------------------------------------------------------------------------


async def test_the_record_outlives_the_trip_that_produced_it(session):
    """Acceptance 2. Deleting last year's trip must not delete what it taught
    us - the reason `learning_signals` carries no foreign key either. Here it
    goes further: the row has no trip id at all to cascade on."""
    trips = TripRepository(session)
    store = CalibrationRepository(session)
    stored = await trips.create(kyoto())
    await store.record_many(automatic_outcomes(stored))

    before = await store.list_for()
    assert len(before) == 1

    await trips.delete(stored.trip_id)

    after = await store.list_for()
    assert [o.outcome_id for o in after] == [o.outcome_id for o in before]
    assert all("trip_id" not in o.model_dump() for o in after)
    # And nothing finer than a region survived with them.
    assert {o.scope for o in after} <= {SCOPE, "unknown"}


# --- 3 ------------------------------------------------------------------------


def test_below_the_minimum_it_makes_no_claim_at_all():
    """Acceptance 3. A bias is a claim. Four journeys do not support one."""
    thin = record(answers(4))

    assert thin.status == "uncalibrated"
    assert thin.bias is None
    assert calibrate(14.0, thin).adjusted is False
    # And it still reports the little it has, so a screen can say so.
    assert thin.sample_count == 4


# --- 4 ------------------------------------------------------------------------


def test_a_borrowed_answer_says_it_is_borrowed():
    """Acceptance 4. "Measured here over 40" and "borrowed from this provider's
    global record over 6" are different claims, and one percentage would hide
    which you were reading."""
    here = record(answers(6))
    assert (here.level, here.scope_used, here.sample_count) == ("scoped", SCOPE, 6)

    elsewhere = record(answers(6), scope="America/Chicago")
    assert elsewhere.level == "mode"
    assert elsewhere.scope_used == ""
    assert elsewhere.bias == here.bias


# --- 5 ------------------------------------------------------------------------


def test_one_bad_journey_is_not_a_verdict():
    """Acceptance 5. A 14-minute estimate that took 95 minutes is a +579%
    error. The mean would report it as the provider's character."""
    ordinary = answers(6)
    closure = outcome_from_measurement(
        predictions_from(kyoto())[0].model_copy(update={"prediction_id": "pred_closure"}),
        95.0,
    )

    assert record(ordinary + [closure]).bias == pytest.approx(record(ordinary).bias, abs=0.01)


# --- 6 ------------------------------------------------------------------------


async def test_the_card_carries_the_band_and_the_count(client, session):
    """Acceptance 6. The figure a provider gave stays exactly what it was; the
    record travels beside it."""
    stored = await TripRepository(session).create(kyoto())
    await CalibrationRepository(session).record_many(answers(6))

    views = (await client.get(f"/trips/{stored.trip_id}/decisions")).json()
    area = next(v for v in views if v["name"] == "hotel_area")
    metric = area["options"][0]["metrics"][0]

    assert metric["value"].startswith("14 min"), "the stored figure is untouched"
    assert "6 checks" in metric["note"]
    assert "8 in 10 landed" in metric["note"]


# --- 7 ------------------------------------------------------------------------


async def test_calibration_never_reorders_a_shortlist(client, session):
    """Acceptance 7. Every option on a card is measured in one route matrix and
    therefore shares a bias, so a correction would multiply them all by the
    same number. Annotating is the consumer; ordering is not."""
    stored = await TripRepository(session).create(kyoto())
    plain = (await client.get(f"/trips/{stored.trip_id}/decisions")).json()

    await CalibrationRepository(session).record_many(answers(12))
    annotated = (await client.get(f"/trips/{stored.trip_id}/decisions")).json()

    def labels(views):
        area = next(v for v in views if v["name"] == "hotel_area")
        return [o["label"] for o in area["options"]]

    assert labels(annotated) == labels(plain)


# --- 8 ------------------------------------------------------------------------


async def test_never_checked_is_said_rather_than_left_blank(client, session):
    """Acceptance 8. Rendering nothing would let "we have never once checked
    whether this is right" and "this is right" look identical."""
    stored = await TripRepository(session).create(kyoto())

    views = (await client.get(f"/trips/{stored.trip_id}/decisions")).json()
    area = next(v for v in views if v["name"] == "hotel_area")
    assert "never checked" in area["options"][0]["metrics"][0]["note"]

    panel = (await client.get("/calibration")).json()
    assert panel["total_checks"] == 0
    assert {r["status"] for r in panel["records"]} == {"uncalibrated"}
    assert set(panel["never_checked"]) == {
        "travel_minutes",
        "hotel_headline_gap",
        "hours_shelf_life",
        "day_high_c",
    }


# --- 9 ------------------------------------------------------------------------


async def test_nothing_here_ever_touches_a_traveller_s_profile(client, session):
    """Acceptance 9. M9 needed consent for every write because it was changing
    what the system thinks of a person. This is about providers - so it needs
    no consent gate, and must be incapable of using one."""
    profiles = ProfileRepository(session)
    profile = await profiles.create(TravelerProfile(profile_id="user_x", name="Haoyu"))
    stored = await TripRepository(session).create(kyoto(ended=True, profile_id="user_x"))

    asked = (await client.get(f"/trips/{stored.trip_id}/overview")).json()["reflection"][
        "estimates"
    ]
    await client.post(
        f"/trips/{stored.trip_id}/reflection",
        json={
            "answered_by": "trv_1",
            "estimates": [
                {"prediction_id": a["prediction_id"], "answer": "much_longer"}
                for a in asked
            ],
        },
    )

    assert len(await CalibrationRepository(session).list_for()) == len(asked) >= 1
    after = await profiles.get("user_x")
    assert after.revision == profile.revision
    assert after.learned == {}


# --- 10 -----------------------------------------------------------------------


async def test_a_fault_measuring_ourselves_never_costs_the_traveller_anything(
    client, session, monkeypatch
):
    """Acceptance 10. The shape ledger 54 established: the derivation runs
    inside the same guard as the write, so a fault here cannot take down the
    reflection somebody just wrote."""
    import app.api.learning as learning_api

    def explode(*_args, **_kwargs):
        raise RuntimeError("the corpus is having a day")

    monkeypatch.setattr(learning_api, "predictions_from", explode)
    stored = await TripRepository(session).create(kyoto(ended=True))

    response = await client.post(
        f"/trips/{stored.trip_id}/reflection",
        json={
            "answered_by": "trv_1",
            "loved": ["the quiet morning"],
            "estimates": [{"prediction_id": "pred_whatever", "answer": "much_longer"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True, "their reflection was saved"
    assert any("estimates" in w for w in body["warnings"]), "and they were told"
    assert await CalibrationRepository(session).list_for() == []
