from datetime import datetime

from app.models import Money, TripConstraint
from app.services import check_hard_constraints
from app.services.constraint_service import check_budget, check_schedule
from tests.conftest import make_item, sample_state


def constraint(category: str, *, type_: str = "hard", **params) -> TripConstraint:
    return TripConstraint(
        id=f"con_{category}",
        category=category,
        description=f"{category} rule",
        type=type_,
        scope="trip",
        source="user_explicit",
        params=params,
    )


# --- budget ------------------------------------------------------------------


def test_budget_ok_when_under_the_ceiling():
    state = sample_state()

    result = check_budget(constraint("budget"), state)

    # 160 total against 2500 x 4 travelers.
    assert result.status == "ok"
    assert "160.00" in result.message


def test_budget_violated_when_over_the_ceiling():
    state = sample_state()

    result = check_budget(constraint("budget", max_total_per_person=10), state)

    assert result.status == "violated"
    assert "over the" in result.message


def test_constraint_params_take_priority_over_the_brief():
    state = sample_state()
    state.brief.budget.total_per_person = 100_000

    assert check_budget(constraint("budget", max_total_per_person=1), state).status == "violated"


def test_budget_not_checkable_without_a_ceiling():
    state = sample_state()
    state.brief.budget.total_per_person = None

    result = check_budget(constraint("budget"), state)

    assert result.status == "not_checkable"
    assert "no per-person budget ceiling" in result.message


def test_budget_not_checkable_without_costs():
    state = sample_state()
    for _day, item in state.itinerary.iter_items():
        item.estimated_cost = None

    assert check_budget(constraint("budget"), state).status == "not_checkable"


def test_budget_not_checkable_across_currencies():
    state = sample_state()
    state.itinerary.days[0].items[0].estimated_cost = Money(amount=5000, currency="JPY")

    result = check_budget(constraint("budget"), state)

    assert result.status == "not_checkable"
    assert "FX conversion not implemented" in result.message


# --- schedule ----------------------------------------------------------------


def test_schedule_ok_when_fixed_items_do_not_collide():
    assert check_schedule(constraint("schedule"), sample_state()).status == "ok"


def test_schedule_detects_overlapping_fixed_items():
    state = sample_state()
    state.itinerary.days[0].items.append(
        make_item(
            "item_bar",
            title="Bar",
            start=datetime(2026, 10, 3, 20, 0),
            end=datetime(2026, 10, 3, 22, 0),
        )
    )

    result = check_schedule(constraint("schedule"), state)

    assert result.status == "violated"
    assert "overlap" in result.message


def test_schedule_detects_an_item_filed_under_the_wrong_day():
    state = sample_state()
    state.itinerary.days[0].items[1].start_at = datetime(2026, 10, 5, 19, 0)
    state.itinerary.days[0].items[1].end_at = datetime(2026, 10, 5, 21, 0)

    result = check_schedule(constraint("schedule"), state)

    assert result.status == "violated"
    assert "filed under" in result.message


def test_flexible_items_are_free_to_overlap():
    state = sample_state()
    state.itinerary.days[0].items[1].time_flexibility = "flexible"
    state.itinerary.days[0].items.append(
        make_item(
            "item_bar",
            title="Bar",
            flexibility="flexible",
            start=datetime(2026, 10, 3, 19, 30),
            end=datetime(2026, 10, 3, 22, 0),
        )
    )

    assert check_schedule(constraint("schedule"), state).status == "not_checkable"


# --- registry ----------------------------------------------------------------


def test_categories_without_a_checker_report_not_checkable():
    state = sample_state()
    state.constraints = [constraint(category) for category in ("flight", "hotel", "mobility")]

    results = check_hard_constraints(state)

    assert {r.status for r in results} == {"not_checkable"}
    assert all("no deterministic checker" in r.message for r in results)


def test_soft_constraints_are_not_hard_checked():
    state = sample_state()
    state.constraints = [constraint("budget", type_="soft", max_total_per_person=1)]

    assert check_hard_constraints(state) == []
