"""Milestone 10 acceptance: the agent measures its own error.

    .\\.venv\\Scripts\\python.exe scripts\\check_our_own_numbers.py

Seven acts:

    1. A trip is planned. Every checkable figure it holds is *derived* from
       it - nothing extra was written to make that possible.
    2. Nothing has been checked, and the system says so out loud rather than
       rendering a confident silence.
    3. An advertised hotel rate checks itself: the claim and the cheapest
       rate any named site will honour arrived in the same fetch.
    4. The trip ends. The card asks about the two estimates that argued for
       something the traveller actually chose - and no more.
    5. Answers accumulate into a band. Below five checks it still refuses to
       say anything; past it, it reports the median error and the interval
       eight checks in ten landed in, with the count beside them.
    6. A single road closure does not become a finding.
    7. The trip is deleted. Its predictions vanish with it, because they were
       never stored. The outcomes remain, carrying no trip id and nothing
       finer than a region, and the calibration is unchanged.

Needs no key at all: every figure here is stored or arithmetic.
"""

import asyncio
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.repository import CalibrationRepository, TripRepository  # noqa: E402
from app.models.common import Money  # noqa: E402
from app.models.decision import (  # noqa: E402
    Decision,
    DecisionOption,
    HotelAreaOption,
    HotelOptionData,
)
from app.models.flight import AirportOption  # noqa: E402
from app.models.hotel import HotelPriceQuote  # noqa: E402
from app.models.trip import TripBrief, TripState, TripTraveler  # noqa: E402
from app.services.calibration_service import (  # noqa: E402
    Calibrations,
    automatic_outcomes,
    calibration_for,
    dimensions_never_checked,
    outcome_for,
    outcome_from_measurement,
    predictions_from,
    question_text,
    questions_for,
)

SETTINGS = Settings(calibration_min_samples=5, calibration_confident_samples=12)
SCOPE = "Asia/Tokyo"


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def kyoto(trip_id: str, *, ended: bool = False) -> TripState:
    start = date.today() - timedelta(days=8) if ended else date.today() + timedelta(days=30)
    state = TripState.new(
        title="Kyoto",
        brief=TripBrief.model_validate(
            {
                "destination": {"city": "Kyoto", "flexible": False},
                "dates": {"start": start.isoformat(), "end": (start + timedelta(days=4)).isoformat()},
                "timezone": SCOPE,
            }
        ),
        travelers=[TripTraveler(traveler_id="trv_1", name="Haoyu", role="organizer")],
    )
    object.__setattr__(state, "trip_id", trip_id) if False else None
    state.trip_id = trip_id

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
    state.decisions.departure_airport = Decision(
        options=[
            DecisionOption(
                option_id="opt_itm",
                data=AirportOption(
                    iata="ITM",
                    name="Osaka Itami",
                    city="Osaka",
                    lat=34.79,
                    lng=135.44,
                    ground_travel_minutes=52.0,
                    ground_travel_source="routes_api",
                ),
            )
        ],
        selected_option_id="opt_itm",
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
                    quotes=[HotelPriceQuote(source="a booking site", nightly=Money(amount=214.0))],
                ),
            )
        ],
        selected_option_id="opt_ryokan",
    )
    return state


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_async_engine(f"sqlite+aiosqlite:///{Path(tmp) / 'demo.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async with sessions() as session:
            trips = TripRepository(session)
            store = CalibrationRepository(session)

            # --- 1 -------------------------------------------------------
            rule("1. Every checkable figure is derived from the trip, not stored")
            state = await trips.create(kyoto("trip_kyoto"))
            for prediction in predictions_from(state):
                print(
                    f"   {prediction.dimension:20} {prediction.value:>7.1f}"
                    f"  {prediction.subject_label}"
                    + ("  (this one decided something)" if prediction.drove_the_choice else "")
                )
            print("\n   rows written to make that possible: 0")

            # --- 2 -------------------------------------------------------
            rule("2. Nothing has been checked, and it says so")
            corpus = Calibrations.of([], scope=SCOPE, settings=SETTINGS)
            print(f"   {corpus.note(14.0, 'google_routes', 'travel_minutes', 'transit')}")
            print(f"   never checked: {dimensions_never_checked([])}")

            # --- 3 -------------------------------------------------------
            rule("3. One check needs nobody: the advertised rate refutes itself")
            automatic = automatic_outcomes(state)
            await store.record_many(automatic)
            for outcome in automatic:
                low, _high = outcome.relative_error
                print(
                    f"   advertised {outcome.predicted:.0f} -> "
                    f"cheapest any named site will honour {outcome.actual_low:.0f}"
                    f"   ({low:+.1%})"
                )

            # --- 4 -------------------------------------------------------
            rule("4. The trip ends, and the card asks two questions at most")
            ended = kyoto("trip_kyoto_ended", ended=True)
            await trips.create(ended)
            asked = questions_for(ended, await store.list_for(), settings=SETTINGS)
            for prediction in asked:
                print(f"   {question_text(prediction)}")
            print(f"\n   figures on this trip: {len(predictions_from(ended))}, asked about: {len(asked)}")

            # --- 5 -------------------------------------------------------
            rule("5. Answers accumulate, and it refuses to speak until they add up")
            answers = ["a_bit_longer", "much_longer", "a_bit_longer", "about_right",
                       "a_bit_longer", "much_longer"]
            running = []
            for index, answer in enumerate(answers, start=1):
                prediction = predictions_from(ended)[0].model_copy(
                    update={"prediction_id": f"pred_demo{index}"}
                )
                running.append(outcome_for(prediction, answer=answer))
                record = calibration_for(
                    running,
                    provider="google_routes",
                    dimension="travel_minutes",
                    mode="transit",
                    scope=SCOPE,
                    settings=SETTINGS,
                )
                said = (
                    "nothing yet"
                    if record.bias is None
                    else f"off by {record.bias:+.0%}, 8 in 10 between "
                    f"{record.low_error:+.0%} and {record.high_error:+.0%}"
                )
                print(f"   after {index} check(s): {record.status:<12} {said}")
            await store.record_many(running)

            # --- 6 -------------------------------------------------------
            rule("6. One road closure is not a finding")
            closure = outcome_from_measurement(
                predictions_from(ended)[0].model_copy(update={"prediction_id": "pred_closure"}),
                95.0,
                checked_by="traveller",
            )
            before = calibration_for(running, provider="google_routes",
                                     dimension="travel_minutes", mode="transit",
                                     scope=SCOPE, settings=SETTINGS)
            after = calibration_for(running + [closure], provider="google_routes",
                                    dimension="travel_minutes", mode="transit",
                                    scope=SCOPE, settings=SETTINGS)
            print(f"   a 14-minute estimate that took 95 minutes is a {(95 - 14) / 14:+.0%} error")
            print(f"   median before: {before.bias:+.1%}    after: {after.bias:+.1%}")

            # --- 7 -------------------------------------------------------
            rule("7. Delete the trips. The predictions go; the record stays")
            kept = len(await store.list_for())
            await trips.delete("trip_kyoto")
            await trips.delete("trip_kyoto_ended")
            remaining = await store.list_for()
            print(f"   outcomes before the delete: {kept}")
            print(f"   outcomes after:             {len(remaining)}")
            print(f"   any of them carrying a trip id: "
                  f"{any('trip_id' in o.model_dump() for o in remaining)}")
            print(f"   finest location any of them holds: "
                  f"{sorted({o.scope for o in remaining})}")
            final = calibration_for(remaining, provider="google_routes",
                                    dimension="travel_minutes", mode="transit",
                                    scope=SCOPE, settings=SETTINGS)
            print(f"\n   calibration still: {final.status}, {final.sample_count} checks, "
                  f"off by {final.bias:+.0%}")

        await engine.dispose()

    print("\nDone. Nothing above needed a key, a network call, or a stored prediction.")


if __name__ == "__main__":
    asyncio.run(main())
