"""What a trip is waiting for, derived from what it holds.

The question three places used to answer differently. These pin the two real
dependencies - a hotel waits on its neighbourhood, flights wait on an airport
the traveller has actually picked - and the rule that a choice already on the
table beats starting new work.
"""

from app.models.decision import Decision, DecisionOption, HotelAreaOption
from app.services.next_step import next_steps, waiting_on_the_agent
from tests.conftest import sample_state


def planning_trip():
    """A confirmed brief with an origin, so airports and flights are in scope."""
    state = sample_state()
    state.intake.status = "confirmed"
    state.brief.origin.city = "San Francisco"
    state.brief.scope.flights = "plan"
    state.brief.scope.lodging = "plan"
    return state


def area_decision(*, selected: bool):
    options = [
        DecisionOption[HotelAreaOption](
            option_id="opt_ueno", data=HotelAreaOption(area_name="Ueno"),
            status="selected" if selected else "shortlisted",
        ),
        DecisionOption[HotelAreaOption](
            option_id="opt_gion", data=HotelAreaOption(area_name="Gion"), status="shortlisted",
        ),
    ]
    return Decision[HotelAreaOption](
        decision_id="dec_area",
        status="selected" if selected else "shortlisted",
        options=options,
        selected_option_id="opt_ueno" if selected else None,
    )


def kinds(state) -> list[tuple[str, str]]:
    return [(s.kind, s.what) for s in next_steps(state)]


# --- the gate ----------------------------------------------------------------


def test_an_unconfirmed_brief_has_no_next_steps():
    """The brief card owns that stage; two things asking at once is one too many."""
    state = sample_state()
    state.intake.status = "collecting"
    state.entities = {}
    state.itinerary.days = []

    assert next_steps(state) == []


# --- choices come first ------------------------------------------------------


def test_a_choice_on_the_table_comes_before_new_work():
    state = planning_trip()
    state.decisions.hotel_area = area_decision(selected=False)

    steps = kinds(state)
    assert ("choose", "hotel_area") in steps
    assert steps.index(("choose", "hotel_area")) < steps.index(("research", "flights"))


def test_a_settled_decision_is_not_still_a_choice():
    state = planning_trip()
    state.decisions.hotel_area = area_decision(selected=True)

    assert ("choose", "hotel_area") not in kinds(state)


# --- the two real dependencies ----------------------------------------------


def test_hotels_wait_for_a_neighbourhood():
    """Mirrors the refusal in hotel_service.resolve_area, which is the rule."""
    state = planning_trip()

    # No area at all: comparing neighbourhoods is the step, not finding a hotel.
    assert ("research", "hotel_area") in kinds(state)
    assert ("research", "hotel") not in kinds(state)

    # An area shortlisted but unchosen still does not unblock hotels.
    state.decisions.hotel_area = area_decision(selected=False)
    assert ("research", "hotel") not in kinds(state)

    # Chosen: now it does.
    state.decisions.hotel_area = area_decision(selected=True)
    assert ("research", "hotel") in kinds(state)


def test_flights_wait_for_an_airport_the_traveller_has_picked():
    state = planning_trip()
    # With no airport decision yet, both airport comparison and the flight
    # search are open - the model may search several origins in one call.
    assert ("research", "flights") in kinds(state)

    # Once airports are on the table, the choice blocks the search.
    state.decisions.departure_airport = area_decision(selected=False)
    assert ("research", "flights") not in kinds(state)
    assert ("choose", "departure_airport") in kinds(state)

    state.decisions.departure_airport = area_decision(selected=True)
    assert ("research", "flights") in kinds(state)


# --- scope and booked --------------------------------------------------------


def test_an_already_arranged_part_is_not_a_next_step():
    state = planning_trip()
    state.brief.scope.flights = "already_arranged"

    steps = kinds(state)
    assert ("research", "flights") not in steps
    assert ("research", "departure_airport") not in steps
    assert ("research", "hotel_area") in steps  # lodging is untouched


def test_a_booked_decision_is_not_shopped_for_again():
    state = planning_trip()
    state.decisions.hotel_area = area_decision(selected=True)
    state.decisions.hotel = Decision[HotelAreaOption](decision_id="dec_hotel", booked=True)

    assert ("research", "hotel") not in kinds(state)


def test_airports_are_not_compared_without_somewhere_to_fly_from():
    state = planning_trip()
    state.brief.origin.city = None
    state.brief.origin.airport_codes = []

    assert ("research", "departure_airport") not in kinds(state)


# --- the ends ----------------------------------------------------------------


def test_an_itinerary_is_the_step_once_there_are_places_and_no_days():
    state = planning_trip()
    # sample_state arrives with a day already laid out, so there is nothing to
    # build; places with no days is the case that wants generating.
    assert ("generate", "itinerary") not in kinds(state)

    state.itinerary.days = []
    assert ("generate", "itinerary") in kinds(state)

    # Places are the raw material: no places, nothing to lay out.
    state.entities = {}
    assert ("generate", "itinerary") not in kinds(state)


def test_a_trip_with_nothing_left_says_so():
    state = planning_trip()
    state.brief.scope.flights = "already_arranged"
    state.brief.scope.lodging = "already_arranged"
    # sample_state already has a day laid out.
    assert next_steps(state) == []


def test_a_choice_is_never_reported_as_work_for_the_agent():
    """`waiting_on_the_agent` is what the auto-continue reads; a card waiting
    on a person must never look like something the agent should get on with."""
    state = planning_trip()
    state.decisions.hotel_area = area_decision(selected=False)

    assert ("choose", "hotel_area") in kinds(state)
    assert all(step.kind != "choose" for step in waiting_on_the_agent(state))
