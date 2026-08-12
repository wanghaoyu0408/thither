"""Milestone 11 acceptance: the stress test / 旅行预演.

    .\\.venv\\Scripts\\python.exe scripts\\preview_the_trip.py

Six acts, entirely offline and deterministic - no database, no key, no model:

    1. A day that validates cleanly still cracks under its own error bars:
       expected arrival makes the reservation, conservative does not.
    2. Every input wears its provenance; the parking buffer is a stated
       assumption with a range, not a number pretending to be measured.
    3. An unmeasured leg advances nothing. The windows downstream say "if the
       schedule held", the day caps at workable, and no lateness is invented.
    4. An earned calibration band (M10) replaces the assumption spread;
       a merely provisional one changes nothing.
    5. "Make this day safer" is the existing scoped replan: the locked dinner
       survives byte-for-byte and the other day does not move a byte.
    6. The verdict vocabulary is four words. There is no score anywhere.
"""

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.models.calibration import Prediction  # noqa: E402
from app.models.itinerary_plan import ReplanParams  # noqa: E402
from app.models.lock import LockRecord  # noqa: E402
from app.models.patch import TripPatch  # noqa: E402
from app.services.calibration_service import Calibrations, outcome_for  # noqa: E402
from app.services.itinerary_service import replan_day  # noqa: E402
from app.services.patch_service import apply_patch  # noqa: E402
from app.services.simulation_service import simulate_trip  # noqa: E402
from app.services.validation_service import TravelLookup  # noqa: E402
from tests.unit.test_simulation_service import (  # noqa: E402
    DAY,
    at,
    measured,
    stop,
    trip,
)

SETTINGS = Settings(calibration_min_samples=5, calibration_confident_samples=12)


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def show_day(day) -> None:
    print(f"   {day.date} · {day.verdict.upper()}   "
          f"({day.legs_measured} of {day.legs_total} journeys measured)")
    for stop_ in day.stops:
        if not stop_.arrival:
            continue
        exp = stop_.arrival["expected"].label()
        cons = stop_.arrival["conservative"].label()
        planned = stop_.scheduled_start.strftime("%H:%M")
        flag = "  · assumes the schedule held earlier" if stop_.rests_on_unknown else ""
        print(f"      {stop_.title}: planned {planned} · expected {exp} · conservative {cons}{flag}")
        for estimate in stop_.inputs:
            print(f"         {estimate.label}")
    for finding in day.findings:
        mark = "⚠" if finding.breaks else "·"
        print(f"      {mark} {finding.kind}: {finding.message}")


def tight_day():
    return trip(
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


def main() -> None:
    # --- 1 -------------------------------------------------------------------
    rule("1. Safe as expected, fragile if the day runs slow")
    state = tight_day()
    forecast = simulate_trip(
        state, travel=measured(("ent_museum", "ent_cafe", 28.0)), issues=[], settings=SETTINGS
    )
    show_day(forecast.days[0])

    # --- 2 -------------------------------------------------------------------
    rule("2. Every input wears its provenance")
    driving = trip(
        stop("Trailhead", at(9), at(10), "ent_far_a"),
        stop("Lookout", at(11), at(12), "ent_far_b"),
        driving=True,
        entities={"ent_far_a": ["park"], "ent_far_b": ["park"]},
    )
    forecast = simulate_trip(
        driving,
        travel=measured(("ent_far_a", "ent_far_b", 25.0), mode="driving"),
        issues=[],
        settings=SETTINGS,
    )
    show_day(forecast.days[0])

    # --- 3 -------------------------------------------------------------------
    rule("3. An unmeasured leg is unknown, never zero")
    forecast = simulate_trip(tight_day(), travel=TravelLookup(), issues=[], settings=SETTINGS)
    show_day(forecast.days[0])

    # --- 4 -------------------------------------------------------------------
    rule("4. An earned band replaces the assumption; a provisional one moves nothing")
    def corpus(n: int) -> Calibrations:
        outcomes = [
            outcome_for(
                Prediction(
                    trip_id="trip_history",
                    provider="google_routes",
                    dimension="travel_minutes",
                    mode="walking",
                    scope="Asia/Tokyo",
                    value=28.0,
                    subject=f"s{i}",
                ),
                answer="a_bit_longer",
            )
            for i in range(n)
        ]
        return Calibrations.of(outcomes, scope="Asia/Tokyo", settings=SETTINGS)

    for label, n in (("provisional (6 checks)", 6), ("calibrated (12 checks)", 12)):
        forecast = simulate_trip(
            tight_day(),
            travel=measured(("ent_museum", "ent_cafe", 28.0)),
            calibrations=corpus(n),
            issues=[],
            settings=SETTINGS,
        )
        lunch = forecast.days[0].stops[1]
        route = next(e for e in lunch.inputs if e.what == "route")
        print(f"   {label}: conservative {lunch.arrival['conservative'].label()}"
              f"   [{route.provenance}] {route.label}")

    # --- 5 -------------------------------------------------------------------
    rule("5. 'Make this day safer' is the existing scoped replan")
    busy = trip(
        stop("One", at(9), at(9, 45), "ent_museum"),
        stop("Two", at(10), at(10, 45), "ent_cafe"),
        stop("Three", at(11), at(11, 45), "ent_museum"),
        stop("Four", at(12), at(12, 45), "ent_cafe"),
        stop("Five", at(13), at(13, 45), "ent_museum"),
        stop("Dinner", at(19), at(20), "ent_cafe", type="restaurant"),
    )
    quiet = busy.itinerary.days[0].model_copy(deep=True)
    quiet.date = DAY + timedelta(days=1)
    quiet.items = [
        stop("Quiet museum", at(11) + timedelta(days=1), at(13) + timedelta(days=1), "ent_museum")
    ]
    busy.itinerary.days.append(quiet)
    busy.brief.dates.end = quiet.date
    dinner = busy.itinerary.days[0].items[-1]
    busy.locks.append(
        LockRecord(
            target_kind="itinerary_item", target_id=dinner.item_id, reason="booked"
        )
    )

    before_quiet = quiet.model_dump(mode="json")
    before_dinner = dinner.model_dump(mode="json")

    proposal = replan_day(busy, DAY, params=ReplanParams(intensity="relaxed"))
    result = apply_patch(
        busy,
        TripPatch(
            base_revision=busy.revision,
            reason="make the day safer",
            actor="user",
            operations=proposal.operations,
            scope=proposal.scope,
        ),
    )
    assert result.applied, [e.message for e in result.errors]
    after = result.state

    survived = next(
        item
        for day in after.itinerary.days
        for item in day.items
        if item.item_id == dinner.item_id
    )
    print(f"   items on the busy day: {len(busy.itinerary.days[0].items)} -> "
          f"{len(after.itinerary.days[0].items)}")
    print(f"   locked dinner byte-identical: "
          f"{survived.model_dump(mode='json') == before_dinner}")
    print(f"   untouched day byte-identical: "
          f"{after.itinerary.days[1].model_dump(mode='json') == before_quiet}")
    print(f"   revisions spent: {after.revision - busy.revision}")

    # --- 6 -------------------------------------------------------------------
    rule("6. Four words, and nothing pretending to be a probability")
    from typing import get_args

    from app.models.simulation import Verdict

    print(f"   the whole verdict vocabulary: {', '.join(get_args(Verdict))}")

    print("\nDone. No database, no key, no model - arithmetic a person could redo.")


if __name__ == "__main__":
    main()
