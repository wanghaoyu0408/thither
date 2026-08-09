"""Milestone 7 acceptance.

    The agent identifies major preference conflicts instead of hiding them
    behind an average score.

Four friends with deliberately incompatible tastes. The failure being guarded
against is arithmetic rather than behavioural - a mean really does erase the
difference between "everyone is mildly pleased" and "three are delighted and one
is miserable" - so most of this is provable offline without a model in the loop.
"""

from datetime import date, datetime

import pytest

from app.models.common import Money
from app.models.decision import Decision, DecisionOption, FlightOptionData, HotelOptionData
from app.models.entity import PlaceEntity
from app.models.group import GroupScore
from app.models.hotel import HotelRating
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary
from app.models.traveler import (
    ActivityPreferences,
    FlightPreferences,
    FoodPreferences,
    HotelPreferences,
    PacePreferences,
    TravelerProfile,
)
from app.models.trip import (
    DestinationSpec,
    OpenQuestion,
    TripBrief,
    TripDates,
    TripDecisions,
    TripState,
    TripTraveler,
)
from app.services.conflict_service import detect_conflicts, unresolved_blocking
from app.services.group_scoring import build_group_score, rank_hotels_for_group
from app.services.integrity_service import check_integrity
from app.services.preference_service import diff_profile, resolve, stale_targets
from app.services.validation_service import validate_itinerary

START = date(2026, 10, 3)


# --- the four friends --------------------------------------------------------


def profile(profile_id: str, name: str, **sections) -> TravelerProfile:
    return TravelerProfile(profile_id=profile_id, revision=1, name=name, **sections)


ANN = profile(
    "u_ann",
    "Ann",
    flight_preferences=FlightPreferences(price_importance=0.9, nonstop_importance=0.2),
    hotel_preferences=HotelPreferences(price_importance=0.9, location_importance=0.3),
    activity_preferences=ActivityPreferences(interests=["nightlife", "shopping"]),
    pace_preferences=PacePreferences(intensity="packed", max_daily_walking_km=16.0),
)

BO = profile(
    "u_bo",
    "Bo",
    flight_preferences=FlightPreferences(price_importance=0.1, nonstop_importance=0.9),
    hotel_preferences=HotelPreferences(price_importance=0.1, location_importance=0.9),
    pace_preferences=PacePreferences(intensity="balanced"),
)

CY = profile(
    "u_cy",
    "Cy",
    food_preferences=FoodPreferences(dietary_restrictions=["vegetarian"]),
    activity_preferences=ActivityPreferences(interests=["museums"], avoided=["nightlife"]),
    pace_preferences=PacePreferences(intensity="relaxed", max_daily_walking_km=5.0),
)

DEE = profile(
    "u_dee",
    "Dee",
    hotel_preferences=HotelPreferences(min_rating=4.5, quiet_importance=0.9),
    pace_preferences=PacePreferences(preferred_start_time="11:00"),
)

PROFILES = {"trv_ann": ANN, "trv_bo": BO, "trv_cy": CY, "trv_dee": DEE}


def group_trip(*, who: tuple[str, ...] = ("ann", "bo", "cy", "dee"), resolved=True) -> TripState:
    state = TripState.new(title="four friends in Tokyo")
    state.brief = TripBrief(
        destination=DestinationSpec(city="Tokyo", country="Japan", flexible=False),
        timezone="Asia/Tokyo",
        dates=TripDates(start=START, end=START + 4 * (date(2026, 10, 4) - date(2026, 10, 3))),
    )

    travelers = []
    for short in who:
        traveler_id = f"trv_{short}"
        traveler = TripTraveler(
            traveler_id=traveler_id,
            name=PROFILES[traveler_id].name,
            profile_id=PROFILES[traveler_id].profile_id,
            role="organizer" if short == "ann" else "member",
        )
        if resolved:
            traveler.preferences = resolve(traveler, PROFILES[traveler_id])
        travelers.append(traveler)

    state.travelers = travelers
    return state


def with_dinner(state: TripState, *, verified: str = "unknown") -> TripState:
    """A scheduled restaurant, with a given dietary attestation."""
    state.entities["ent_dinner"] = PlaceEntity(
        entity_id="ent_dinner",
        name="Yakitori Ichidai",
        categories=["restaurant"],
        lat=35.7148,
        lng=139.7967,
        serves_vegetarian=verified,
    )
    state.itinerary = TripItinerary(
        days=[
            ItineraryDay(
                date=START,
                items=[
                    ItineraryItem(
                        item_id="item_dinner",
                        type="restaurant",
                        entity_id="ent_dinner",
                        title="Dinner",
                        start_at=datetime(2026, 10, 3, 19, 0),
                        end_at=datetime(2026, 10, 3, 21, 0),
                    )
                ],
            )
        ]
    )
    return state


def hotel(name: str, *, nightly: float, rating: float | None = None, reviews: int = 800):
    ratings = []
    if rating is not None:
        ratings.append(
            HotelRating(
                value=rating, type="user_rating", source="google_hotels", review_count=reviews
            )
        )
    return HotelOptionData(
        provider="fixture",
        offer_ref=name.lower().replace(" ", "_"),
        live_mode=True,
        name=name,
        nightly_price=Money(amount=nightly),
        ratings=ratings,
    )


# --- the acceptance ----------------------------------------------------------


def test_a_split_and_a_consensus_with_the_same_mean_are_told_apart():
    """The whole milestone in one assertion.

    Three delighted travellers and one miserable one average higher than four
    mildly-pleased ones. A mean would recommend the first. Nothing here does.
    """
    names = {"a": "Ann", "b": "Bo", "c": "Cy", "d": "Dee"}
    split = build_group_score({"a": 0.9, "b": 0.9, "c": 0.9, "d": 0.1}, names)
    flat = build_group_score({"a": 0.55, "b": 0.55, "c": 0.55, "d": 0.55}, names)

    # A mean prefers the option that ruins Dee.
    assert split.mean > flat.mean

    # Nothing this project sorts on does.
    assert split.total < flat.total
    assert split.is_split and not flat.is_split
    assert split.worst_traveler_id == "d"

    # And the split cannot be described without saying so.
    assert "Dee" in split.describe()
    assert "split" in split.describe()
    assert "Dee" not in flat.describe()


def test_a_group_score_never_renders_a_split_as_a_bare_number():
    split = build_group_score({"a": 0.95, "b": 0.15}, {"a": "Ann", "b": "Bo"})

    described = split.describe()

    assert "0.15" in described and "Bo" in described
    # The per-traveler scores survive into the record itself, not just the text.
    assert split.per_traveler == {"a": 0.95, "b": 0.15}
    assert split.ranked_travelers()[0] == ("b", 0.15)


def test_the_fairness_penalty_costs_an_option_for_ruining_someone():
    """Two hotels, same price and rating; one is unbearable for Dee."""
    cheap = hotel("Bargain Inn", nightly=90.0, rating=3.2)
    decent = hotel("Fine Hotel", nightly=95.0, rating=4.6)

    state = group_trip()
    travelers = {t.traveler_id: t.preferences for t in state.travelers}
    names = {t.traveler_id: t.name for t in state.travelers}

    ranked = rank_hotels_for_group([cheap, decent], travelers=travelers, names=names)
    best = ranked[0]

    # Dee asked for 4.5+; the 3.2 is what she is worst served by.
    worst_on_cheap = next(item for item in ranked if item.option.name == "Bargain Inn")
    assert worst_on_cheap.group.worst_traveler_id == "trv_dee"
    assert best.option.name == "Fine Hotel"
    # The loser is named rather than merely outvoted.
    assert any("Dee" in con for con in worst_on_cheap.cons)


def test_conflicts_name_both_sides_rather_than_averaging_them():
    conflicts = detect_conflicts(group_trip())
    kinds = {conflict.kind for conflict in conflicts}

    assert "importance" in kinds
    assert "pace" in kinds
    assert "interest" in kinds

    price = next(c for c in conflicts if c.kind == "importance" and "flight price" in c.summary)
    # Ann at 0.9 and Bo at 0.1 - both stances recorded, neither merged.
    assert set(price.positions) == {"trv_ann", "trv_bo"}
    assert "0.9" in price.positions["trv_ann"]
    assert "0.1" in price.positions["trv_bo"]
    assert "Averaging them" in price.summary


def test_opposite_interests_are_reported_with_who_wants_what():
    conflict = next(c for c in detect_conflicts(group_trip()) if c.kind == "interest")

    assert conflict.positions["trv_ann"] == "interested in nightlife"
    assert conflict.positions["trv_cy"] == "asked to avoid nightlife"
    assert conflict.resolution_options


def test_a_relaxed_and_a_packed_traveler_conflict_on_pace():
    conflicts = [c for c in detect_conflicts(group_trip()) if c.kind == "pace"]

    assert any("packed" in c.summary and "relaxed" in c.summary for c in conflicts)
    # Ann walks 16 km a day, Cy tops out at 5.
    assert any("16" in c.summary and "5" in c.summary for c in conflicts)


@pytest.mark.parametrize("worst_weight", [0.0, 0.4, 1.0])
def test_conflicts_do_not_depend_on_the_ranking_formula(worst_weight):
    """Conflicts come from what people want, never from how options are scored.

    A group that disagrees disagrees whether the formula is a plain mean or
    pure maximin. If the fairness weight could change what gets *reported*,
    tuning the ranker would quietly change which arguments the group is told
    about - which is the averaging failure wearing a different hat.
    """
    from app.services.group_scoring import build_group_score

    state = with_dinner(group_trip(), verified="unknown")
    baseline = detect_conflicts(state)

    # Scoring anything at all under this weight must not perturb the trip.
    build_group_score({"trv_ann": 0.9, "trv_cy": 0.1}, worst_weight=worst_weight)
    again = detect_conflicts(state)

    assert [c.conflict_id for c in again] == [c.conflict_id for c in baseline]
    assert [c.summary for c in again] == [c.summary for c in baseline]
    assert [c.positions for c in again] == [c.positions for c in baseline]


def test_the_configured_weight_reaches_the_group_ranker():
    cheap = hotel("Bargain Inn", nightly=90.0, rating=3.2)
    state = group_trip()
    travelers = {t.traveler_id: t.preferences for t in state.travelers}
    names = {t.traveler_id: t.name for t in state.travelers}

    maximin = rank_hotels_for_group([cheap], travelers=travelers, names=names, worst_weight=1.0)[0]

    assert maximin.group.worst_weight == 1.0
    # Pure maximin: the group verdict *is* the unhappiest traveller's score.
    assert maximin.group.total == maximin.group.worst


def test_a_group_that_agrees_produces_no_conflicts():
    state = group_trip(who=("bo",))

    assert detect_conflicts(state) == []


# --- you cannot recommend what nobody has looked at --------------------------


def test_an_option_cannot_win_on_ignorance():
    """Renormalizing alone lets an option win on ignorance.

    Found on live data: a hotel with three reviews has no usable guest rating,
    no star category and no measured travel time, so price was its only
    dimension - and being cheapest made it a flawless 1.00 that beat a hotel we
    knew five things about.

    The fix is in the *ordering*, not the score. `total` still reports what the
    one known dimension said; `ranking_value` is what refuses to rank it first.
    """
    from app.services.scoring import combine, ranking_value

    thin = combine({"price": (1.0, 0.5), "rating": (None, 0.5), "location": (None, 1.0)})
    full = combine({"price": (0.8, 0.5), "rating": (0.8, 0.5), "location": (0.8, 1.0)})

    assert thin.coverage == 0.25
    assert full.coverage == 1.0

    # The stored score is not doctored: price really was 1.0 for the thin one.
    assert thin.total == 1.0
    assert full.total == 0.8

    # But a perfect score on a quarter of the evidence does not outrank a good
    # one on all of it.
    assert ranking_value(thin) < ranking_value(full)
    assert "25% of the usual evidence" in thin.notes


def test_score_and_confidence_are_never_folded_together():
    """The stored number says what the evidence says; coverage says how much.

    Two dimensions known at 0.8 and 0.8 is a 0.8 whether it is two of two or
    two of ten - what differs is `coverage`, and that difference must not be
    smuggled into the score.
    """
    from app.services.scoring import combine, ranking_value

    scored = combine({"a": (0.8, 1.0), "b": (0.6, 1.0)})
    assert scored.coverage == 1.0
    assert scored.total == 0.7
    # Full evidence: ordering and score agree exactly.
    assert ranking_value(scored) == scored.total

    partial = combine({"a": (0.8, 1.0), "b": (0.8, 1.0), "c": (None, 8.0)})
    assert partial.total == 0.8
    assert partial.coverage == 0.2
    assert ranking_value(partial) < partial.total


def test_a_group_cannot_agree_about_something_nobody_knows():
    from app.services.group_scoring import group_ranking_value

    thin = build_group_score({"a": 0.9, "b": 0.9}, {"a": "Ann", "b": "Bo"}, coverage=0.1)

    assert thin.thinly_evidenced
    assert "nobody has much to go on" in thin.describe()
    assert "10% of the usual evidence" in thin.describe()
    # The verdict still reports what they scored; the ordering is what pulls back.
    assert thin.total == 0.9
    assert group_ranking_value(thin) < 0.9


def test_a_thin_rating_cannot_clear_a_stated_floor():
    """Too thin to score on and good enough to pass a requirement is incoherent."""
    unproven = hotel("Brand New", nightly=100.0, rating=5.0, reviews=3)
    proven = hotel("Well Known", nightly=100.0, rating=4.6, reviews=900)

    state = group_trip(who=("dee",))
    travelers = {t.traveler_id: t.preferences for t in state.travelers}

    ranked = rank_hotels_for_group(
        [unproven, proven], travelers=travelers, names={"trv_dee": "Dee"}
    )
    by_name = {item.option.name: item for item in ranked}

    assert (
        "meets_stated_minimum" not in by_name["Brand New"].per_traveler["trv_dee"].score.dimensions
    )
    assert (
        by_name["Well Known"].per_traveler["trv_dee"].score.dimensions["meets_stated_minimum"]
        == 1.0
    )
    assert ranked[0].option.name == "Well Known"


# --- dietary: unknown is not a violation -------------------------------------


def test_an_unverified_restaurant_raises_a_question_and_is_not_removed():
    """Google confirms a place *does* serve vegetarian food, never that it does not.

    So an unattested restaurant is unverified, not unsuitable, and deleting it
    would be a confident claim on no evidence.
    """
    state = with_dinner(group_trip(), verified="unknown")

    conflict = next(c for c in detect_conflicts(state) if c.kind == "dietary")

    assert conflict.severity == "blocking"
    assert conflict.traveler_ids == ["trv_cy"]
    assert "not confirmed" in conflict.summary
    assert "never confirms the opposite" in conflict.summary
    assert conflict.affects == ["item_dinner"]

    # Still on the itinerary. Nothing was filtered.
    assert [item.item_id for _day, item in state.itinerary.iter_items()] == ["item_dinner"]


def test_a_confirmed_restaurant_clears_the_conflict():
    state = with_dinner(group_trip(), verified="confirmed_true")

    assert not any(c.kind == "dietary" for c in detect_conflicts(state))


def test_a_confirmed_denial_is_a_different_conflict_from_an_unknown():
    """The whole point of the third state.

    "Nobody checked" asks the group to check. "The provider says no" asks them
    to swap. Collapsing the two into one bool made both read as the first.
    """
    state = with_dinner(group_trip(), verified="confirmed_false")

    conflict = next(c for c in detect_conflicts(state) if c.kind == "dietary")

    assert conflict.severity == "blocking"
    assert "confirmed unsuitable" in conflict.summary
    assert "positive assertion" in conflict.summary
    # Swap, not verify - there is nothing left to verify.
    assert any("swap" in option for option in conflict.resolution_options)
    assert not any("check the menu" in option for option in conflict.resolution_options)


def test_unknown_and_confirmed_denials_are_reported_separately():
    state = with_dinner(group_trip(), verified="confirmed_false")
    # A second meal nobody has checked.
    state.entities["ent_lunch"] = PlaceEntity(
        entity_id="ent_lunch",
        name="Unchecked Diner",
        categories=["restaurant"],
        lat=35.7,
        lng=139.8,
    )
    state.itinerary.days[0].items.append(
        ItineraryItem(item_id="item_lunch", type="restaurant", entity_id="ent_lunch", title="Lunch")
    )

    dietary = [c for c in detect_conflicts(state) if c.kind == "dietary"]

    assert len(dietary) == 2
    assert {c.affects[0] for c in dietary} == {"item_dinner", "item_lunch"}
    # And they keep separate identities, so answering one does not settle the other.
    assert len({c.conflict_id for c in dietary}) == 2


def test_a_traveler_with_no_restrictions_raises_nothing():
    state = with_dinner(group_trip(who=("ann", "bo")), verified="unknown")

    assert not any(c.kind == "dietary" for c in detect_conflicts(state))


# --- blocking conflicts stop "ready", and nothing else -----------------------


def test_a_trip_cannot_be_marked_ready_over_an_unanswered_conflict():
    state = with_dinner(group_trip(), verified="unknown")
    state.status = "ready"

    problems = check_integrity(state)

    assert any("cannot be marked ready" in problem for problem in problems)


def test_answering_the_question_lets_the_trip_be_ready():
    state = with_dinner(group_trip(), verified="unknown")
    conflict = next(c for c in detect_conflicts(state) if c.kind == "dietary")
    state.open_questions = [
        OpenQuestion(
            question=f"[{conflict.conflict_id}] {conflict.question()}",
            blocking=True,
            answered=True,
            answer="Cy called ahead; they will do a vegetable skewer set.",
        )
    ]
    state.status = "ready"

    assert check_integrity(state) == []


def test_a_planning_trip_with_a_blocking_conflict_is_still_valid_to_work_on():
    """Blocking stops the claim of readiness, never the work."""
    state = with_dinner(group_trip(), verified="unknown")
    state.status = "planning"

    assert check_integrity(state) == []


def test_the_validator_reports_the_conflict_with_every_position():
    state = with_dinner(group_trip(), verified="unknown")

    issues = [i for i in validate_itinerary(state).issues if i.type == "preference_conflict"]

    blocking = [issue for issue in issues if issue.severity == "error"]
    assert blocking, "a blocking conflict should be an error"
    assert "trv_cy" in blocking[0].message
    assert blocking[0].suggested_fix

    # Material disagreements are reported too, at a lower severity.
    assert any(issue.severity == "warning" for issue in issues)


def test_the_model_sees_conflicts_and_preferences_every_turn():
    from app.agent.context import summarize

    view = summarize(with_dinner(group_trip(), verified="unknown"))

    assert view["preference_conflicts"], "conflicts must be in the summary, not behind a tool"
    assert any(c["severity"] == "blocking" for c in view["preference_conflicts"])
    assert all("positions" in c for c in view["preference_conflicts"])

    ann = next(t for t in view["travelers"] if t["name"] == "Ann")
    assert ann["preferences"]["flight"]["price"] == 0.9
    cy = next(t for t in view["travelers"] if t["name"] == "Cy")
    assert cy["preferences"]["food"]["dietary_restrictions"] == ["vegetarian"]


# --- profiles, overrides and the snapshot ------------------------------------


def test_a_trip_override_beats_the_profile_and_is_recorded():
    traveler = TripTraveler(
        traveler_id="trv_ann",
        name="Ann",
        profile_id="u_ann",
        profile_overrides={"flight": {"price_importance": 0.1}},
    )

    preferences = resolve(traveler, ANN)

    assert preferences.flight.price_importance == 0.1
    # And the rest of the profile survives field-wise.
    assert preferences.flight.nonstop_importance == 0.2
    assert preferences.overridden_paths == ["flight.price_importance"]


def test_dotted_and_nested_overrides_mean_the_same_thing():
    dotted = TripTraveler(
        traveler_id="t", name="Ann", profile_overrides={"flight.price_importance": 0.3}
    )
    nested = TripTraveler(
        traveler_id="t", name="Ann", profile_overrides={"flight": {"price_importance": 0.3}}
    )

    assert resolve(dotted, ANN).flight.price_importance == 0.3
    assert resolve(nested, ANN).flight.price_importance == 0.3


def test_the_snapshot_names_the_profile_revision_it_came_from():
    preferences = resolve(TripTraveler(traveler_id="t", name="Ann", profile_id="u_ann"), ANN)

    assert preferences.source_profile_id == "u_ann"
    assert preferences.source_profile_revision == 1


def test_editing_a_profile_afterwards_changes_nothing_about_the_trip():
    """The reason preferences are snapshotted at all.

    A trip has to stay explainable after the profile behind it has moved on.
    """
    state = group_trip()
    before = state.travelers[0].preferences.model_copy(deep=True)

    moved = ANN.model_copy(
        update={"revision": 9, "flight_preferences": FlightPreferences(price_importance=0.0)}
    )
    assert moved.flight_preferences.price_importance == 0.0

    assert state.travelers[0].preferences == before
    assert state.travelers[0].preferences.flight.price_importance == 0.9


def test_an_unresolved_traveler_is_reported_rather_than_defaulted():
    from app.services.preference_service import effective

    state = group_trip(resolved=False)

    resolved, warnings = effective(state)

    assert len(resolved) == 4
    assert len(warnings) == 4
    assert all("no resolved preferences" in warning for warning in warnings)


# --- refresh: diff, confirm, narrow staleness --------------------------------


def moved_profile() -> TravelerProfile:
    return ANN.model_copy(
        update={
            "revision": 2,
            "flight_preferences": FlightPreferences(price_importance=0.9, nonstop_importance=0.9),
        }
    )


def test_a_diff_reports_what_would_move_and_applies_nothing():
    state = group_trip()
    traveler = state.travelers[0]
    before = traveler.preferences.model_copy(deep=True)

    diff = diff_profile(traveler, moved_profile())

    assert diff.has_effect
    assert [change.path for change in diff.changes] == ["flight.nonstop_importance"]
    assert diff.changes[0].before == 0.2
    assert diff.changes[0].after == 0.9
    assert diff.affects == ["flights"]
    # Untouched.
    assert traveler.preferences == before


def test_an_overridden_field_is_left_alone_and_said_to_be():
    traveler = TripTraveler(
        traveler_id="trv_ann",
        name="Ann",
        profile_id="u_ann",
        profile_overrides={"flight.price_importance": 0.4},
    )
    traveler.preferences = resolve(traveler, ANN)

    diff = diff_profile(
        traveler,
        ANN.model_copy(
            update={"revision": 2, "flight_preferences": FlightPreferences(price_importance=0.0)}
        ),
    )

    # The override pins it, so the effective value did not move.
    assert not any(change.path == "flight.price_importance" for change in diff.changes)
    assert "flight.price_importance" in diff.not_refreshed
    assert "overridden by this trip" in diff.describe()


def test_staleness_is_narrow():
    """A changed flight preference says nothing about the restaurants."""
    state = group_trip()
    state.decisions = TripDecisions(
        flights=Decision[FlightOptionData](decision_id="dec_f"),
        hotel=Decision[HotelOptionData](decision_id="dec_h"),
    )

    diff = diff_profile(state.travelers[0], moved_profile())
    decisions, days = stale_targets(state, [diff])

    assert decisions == ["flights"]
    assert days == []


def test_a_diff_with_no_effect_marks_nothing_stale():
    state = group_trip()
    state.decisions = TripDecisions(flights=Decision[FlightOptionData](decision_id="dec_f"))

    unchanged = diff_profile(state.travelers[0], ANN)
    assert not unchanged.has_effect
    assert stale_targets(state, [unchanged]) == ([], [])


# --- conflicts are derived ---------------------------------------------------


def test_changing_a_preference_changes_the_conflicts_with_no_patch_between():
    state = group_trip()
    assert any(c.kind == "interest" for c in detect_conflicts(state))

    cy = next(t for t in state.travelers if t.name == "Cy")
    cy.preferences = cy.preferences.model_copy(
        update={"activity": ActivityPreferences(interests=["museums"])}
    )

    assert not any(c.kind == "interest" for c in detect_conflicts(state))


def test_unresolved_blocking_ignores_conflicts_the_group_has_settled():
    state = with_dinner(group_trip(), verified="unknown")
    assert unresolved_blocking(state)

    conflict = next(c for c in detect_conflicts(state) if c.kind == "dietary")
    state.open_questions = [
        OpenQuestion(
            question=f"[{conflict.conflict_id}] checked?",
            blocking=True,
            answered=True,
            answer="yes",
        )
    ]

    assert unresolved_blocking(state) == []


# --- the agent's own path ----------------------------------------------------


class FakeProfiles:
    def __init__(self, profiles: dict[str, TravelerProfile]):
        self.by_id = {p.profile_id: p for p in profiles.values()}

    async def get(self, profile_id: str) -> TravelerProfile:
        from app.db.repository import ProfileNotFound

        if profile_id not in self.by_id:
            raise ProfileNotFound(profile_id)
        return self.by_id[profile_id]


def tool_context(state: TripState):
    from app.agent.tool_registry import ToolContext
    from app.config import Settings
    from app.services.proposal_store import ProposalStore

    return ToolContext(
        state=state,
        toolbox=None,
        proposals=ProposalStore(),
        settings=Settings(),
        profiles=FakeProfiles(PROFILES),
    )


async def test_the_review_tool_resolves_and_reports_every_side():
    from app.agent.tool_registry import _review_group_preferences

    context = tool_context(with_dinner(group_trip(resolved=False), verified="unknown"))

    reply = await _review_group_preferences(context, {})

    assert sorted(reply["resolved_this_turn"]) == ["Ann", "Bo", "Cy", "Dee"]
    assert reply["blocking_count"] >= 1
    assert all(conflict["positions"] for conflict in reply["conflicts"])
    # Staged, not written.
    assert len(context.pending_traveler_prefs) == 4
    assert context.state.travelers[0].preferences is None
    # A blocking conflict becomes something the group has to answer.
    assert context.pending_questions


async def test_a_solo_trip_gets_none_of_this_machinery():
    """Found live: reviewing group preferences derailed a one-person plan.

    A group of one has nobody to disagree with, so the tool answers immediately
    instead of spending a planning round establishing that.
    """
    from app.agent.tool_registry import _review_group_preferences

    context = tool_context(group_trip(who=("ann",), resolved=False))

    reply = await _review_group_preferences(context, {})

    assert reply["conflicts"] == []
    assert reply["blocking_count"] == 0
    assert "travelling alone" in reply["note"]
    assert "Get on with planning" in reply["note"]
    # And it spent nothing: no profile lookup, nothing staged.
    assert context.pending_traveler_prefs == {}
    assert context.pending_questions == []


async def test_places_gathered_this_turn_commit_without_an_itinerary_proposal():
    """Found live: the model discovers thirty places, then commits them.

    An earlier version built the entity operations only on the branch that had
    a proposal, so committing without one silently threw the discoveries away
    and reported "no such proposal" - and the agent, having lost its work,
    described a plan it had not saved.
    """
    from app.agent.tool_registry import _apply_trip_patch

    context = tool_context(group_trip())
    context.pending_entity_ops.append(
        PlaceEntity(entity_id="ent_new", name="Somewhere New", lat=35.7, lng=139.8)
    )

    result = await _apply_trip_patch(context, {"reason": "store what I found"})

    paths = [op["path"] for op in result["__patches__"][0]["operations"]]
    assert "/entities/ent_new" in paths


async def test_apply_still_refuses_with_nothing_staged_at_all():
    from app.agent.tool_registry import _apply_trip_patch

    result = await _apply_trip_patch(
        tool_context(group_trip()), {"proposal_id": "nope", "reason": "x"}
    )

    assert result["applied"] is False
    assert "no such proposal" in result["error"]


async def test_a_missing_proposal_id_is_an_error_even_with_places_staged():
    """Committing the places instead would hide that the itinerary never landed.

    The model asked for a specific proposal. Saving something else and calling
    it success is the failure-wearing-a-success's-clothes shape this project
    keeps finding.
    """
    from app.agent.tool_registry import _apply_trip_patch

    context = tool_context(group_trip())
    context.pending_entity_ops.append(
        PlaceEntity(entity_id="ent_new", name="Somewhere New", lat=35.7, lng=139.8)
    )

    result = await _apply_trip_patch(context, {"proposal_id": "gone", "reason": "x"})

    assert result["applied"] is False
    assert "nothing was applied" in result["error"]
    assert "__patches__" not in result


async def test_refresh_applies_nothing_until_confirmed():
    from app.agent.tool_registry import _refresh_traveler_preferences

    context = tool_context(group_trip())
    context.profiles = FakeProfiles({**PROFILES, "trv_ann": moved_profile()})

    first = await _refresh_traveler_preferences(context, {})

    assert first["applied"] is False
    assert context.pending_traveler_prefs == {}
    assert any(diff["changes"] for diff in first["diffs"])

    second = await _refresh_traveler_preferences(context, {"confirm": True})

    assert second["applied"] is True
    assert "trv_ann" in context.pending_traveler_prefs
    assert second["stale_decisions"] == []  # this trip has no decisions yet


async def test_both_group_tools_are_registered():
    from app.agent.tool_registry import HANDLERS, TOOL_SCHEMAS

    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert {"review_group_preferences", "refresh_traveler_preferences"} <= names
    assert {"review_group_preferences", "refresh_traveler_preferences"} <= set(HANDLERS)

    review = next(s for s in TOOL_SCHEMAS if s["name"] == "review_group_preferences")
    assert "NEVER report a group score without also reporting its split" in review["description"]


def test_places_are_scored_per_traveler_too():
    """An adventurous eater and a cautious one do not want the same restaurant."""
    from app.models.place import PlaceSummary
    from app.services.group_scoring import rank_places_for_group

    crowd_pleaser = PlaceSummary(
        place_id="p_safe", name="Safe Bet", rating=4.6, rating_count=4000, price_level=2
    )
    cult_favourite = PlaceSummary(
        place_id="p_cult", name="Tiny Counter", rating=4.2, rating_count=60, price_level=2
    )

    state = group_trip(who=("ann", "cy"))
    # Cy will try anything; Ann wants the sure thing.
    cy = next(t for t in state.travelers if t.name == "Cy")
    cy.preferences = cy.preferences.model_copy(
        update={"food": FoodPreferences(adventurousness=1.0)}
    )
    ann = next(t for t in state.travelers if t.name == "Ann")
    ann.preferences = ann.preferences.model_copy(
        update={"food": FoodPreferences(adventurousness=0.0)}
    )

    ranked = rank_places_for_group(
        [crowd_pleaser, cult_favourite],
        travelers={t.traveler_id: t.preferences for t in state.travelers},
        names={t.traveler_id: t.name for t in state.travelers},
    )
    by_name = {item.option.name: item for item in ranked}

    # The same place is worth different amounts to each of them.
    assert (
        by_name["Safe Bet"].group.per_traveler["trv_ann"]
        != by_name["Safe Bet"].group.per_traveler["trv_cy"]
    )
    assert all(len(item.group.per_traveler) == 2 for item in ranked)


def test_the_prompt_forbids_the_averaging_failure():
    from app.agent.prompts import SYSTEM_INSTRUCTIONS

    assert "Never present a group score without its split" in SYSTEM_INSTRUCTIONS
    assert "unverified" in SYSTEM_INSTRUCTIONS


# --- the patch pipeline ------------------------------------------------------


async def test_preferences_and_questions_reach_trip_state(session):
    from app.agent.tool_registry import _apply_trip_patch, _review_group_preferences
    from app.db.repository import TripRepository
    from app.models.patch import TripPatch

    repository = TripRepository(session)
    stored = await repository.create(with_dinner(group_trip(resolved=False), verified="unknown"))

    context = tool_context(stored)
    await _review_group_preferences(context, {})
    staged = await _apply_trip_patch(context, {"reason": "who wants what"})

    applied = await repository.apply_patch(
        stored.trip_id,
        TripPatch(
            base_revision=stored.revision,
            reason="who wants what",
            actor="agent",
            operations=staged["__patches__"][0]["operations"],
        ),
    )

    assert applied.applied, applied.errors
    saved = applied.state
    assert all(traveler.preferences is not None for traveler in saved.travelers)
    assert saved.travelers[0].preferences.source_profile_revision == 1
    assert any(question.blocking for question in saved.open_questions)
    # And the conflict is now derivable from stored state alone.
    assert any(c.kind == "dietary" for c in detect_conflicts(saved))


@pytest.mark.parametrize("status", ["planning", "draft"])
async def test_work_continues_while_a_conflict_stands(session, status):
    from app.db.repository import TripRepository
    from app.models.patch import TripPatch

    repository = TripRepository(session)
    stored = await repository.create(with_dinner(group_trip(), verified="unknown"))

    applied = await repository.apply_patch(
        stored.trip_id,
        TripPatch(
            base_revision=stored.revision,
            reason="keep planning",
            actor="agent",
            operations=[{"op": "set", "path": "/status", "value": status}],
        ),
    )

    assert applied.applied, applied.errors


async def test_marking_ready_is_refused_by_the_patch_engine(session):
    from app.db.repository import TripRepository
    from app.models.patch import TripPatch

    repository = TripRepository(session)
    stored = await repository.create(with_dinner(group_trip(), verified="unknown"))

    applied = await repository.apply_patch(
        stored.trip_id,
        TripPatch(
            base_revision=stored.revision,
            reason="call it done",
            actor="agent",
            operations=[{"op": "set", "path": "/status", "value": "ready"}],
        ),
    )

    assert not applied.applied
    assert any(error.code == "INTEGRITY_ERROR" for error in applied.errors)


def test_a_group_score_survives_into_a_stored_decision():
    option = DecisionOption[HotelOptionData](
        option_id="opt_a",
        data=hotel("Fine Hotel", nightly=95.0, rating=4.6),
        group_score=build_group_score({"a": 0.9, "b": 0.2}, {"a": "Ann", "b": "Bo"}),
    )
    decision = Decision[HotelOptionData](decision_id="dec_h", options=[option])

    revived = Decision[HotelOptionData].model_validate(decision.model_dump(mode="json"))
    restored = revived.options[0].group_score

    assert isinstance(restored, GroupScore)
    assert restored.is_split
    assert "Bo" in restored.describe()
