"""Flights and airports must survive the turn that found them.

Both were ranked, described to the model, and then dropped on the floor:
`_search_flights` kept its offers in `context.flight_options` and
`_search_airports` in `context.airports`, neither of which outlives the turn.
The `decisions.flights` slot and its `flights_updated` audit event had existed
since M5 with nothing writing them.

The cost was not a missing feature but a dishonest one: a trip could carry a
chosen flight with no stored reason, and "why this one?" had no answer that was
not invented after the fact.
"""

from datetime import UTC, date, datetime, timedelta

from app.agent.tool_registry import ToolContext, _search_airports, _search_flights
from app.config import Settings
from app.models.common import Money
from app.models.decision import Decision, DecisionOption, FlightOptionData
from app.models.flight import AirportOption, FlightSegment, FlightSlice
from app.models.tool import ToolResult
from app.services.proposal_store import ProposalStore
from tests.conftest import sample_state

DEPART = date(2026, 10, 3)


def airport(iata: str, minutes: float | None, source: str = "routes_api") -> AirportOption:
    return AirportOption(
        iata=iata,
        name=f"{iata} International",
        city=iata,
        lat=37.6,
        lng=-122.4,
        ground_travel_minutes=minutes,
        ground_travel_source=source,
    )


def offer(
    ref: str, origin: str, price: float, stops: int, depart_hour: int = 11
) -> FlightOptionData:
    depart = datetime(2026, 10, 3, depart_hour, 0)
    arrive = depart + timedelta(hours=11)
    return FlightOptionData(
        provider="duffel",
        offer_ref=ref,
        live_mode=True,
        price=Money(amount=price * 2),
        price_per_person=Money(amount=price),
        origin=origin,
        destination="HND",
        departure_at=depart,
        arrival_at=arrive,
        duration_minutes=660,
        stops=stops,
        airlines=["UA"],
        slices=[
            FlightSlice(
                origin=origin,
                destination="HND",
                departing_at=depart,
                arriving_at=arrive,
                duration_minutes=660,
                segments=[
                    FlightSegment(
                        origin=origin,
                        destination="HND",
                        departing_at=depart,
                        arriving_at=arrive,
                        marketing_carrier="UA",
                    )
                ],
            )
        ],
        observed_at=datetime.now(UTC),
    )


class FakeFlights:
    def __init__(self, offers):
        self._offers = offers

    async def search_flights(self, spec):
        return ToolResult[FlightOptionData](source="duffel", results=self._offers)


class FakeAirports:
    def __init__(self, options):
        self._options = options

    async def search_airports(self, spec):
        return ToolResult[AirportOption](source="ourairports", results=self._options)


class FakeToolbox:
    def __init__(self, *, flights=None, airports=None):
        self.flights = flights
        self.airports = airports


def context_for(toolbox, state=None) -> ToolContext:
    return ToolContext(
        state=state or sample_state(),
        toolbox=toolbox,
        proposals=ProposalStore(),
        settings=Settings(),
    )


# --- flights -----------------------------------------------------------------


async def test_a_flight_search_stages_a_decision_with_its_reasoning():
    offers = [offer("off_a", "SFO", 742, 0), offer("off_b", "SJC", 656, 1)]
    context = context_for(FakeToolbox(flights=FakeFlights(offers)))

    reply = await _search_flights(
        context,
        {"origins": ["SFO", "SJC"], "destinations": ["HND"], "departure_date": DEPART.isoformat()},
    )

    assert reply["offers"], reply
    assert "flights" in context.pending_decisions, "the ranking died with the turn again"

    decision = context.pending_decisions["flights"]
    assert decision["status"] == "shortlisted"
    assert len(decision["options"]) == 2
    # The reasoning travels with the option, not just the fare.
    best = decision["options"][0]
    assert best["score"]["total"] is not None
    assert best["pros"], "an option with no stated reason cannot be explained later"
    assert best["data"]["offer_ref"] == reply["offers"][0]["offer_ref"]
    assert best["data"]["live_mode"] is True


async def test_a_flight_search_that_finds_nothing_stages_nothing():
    context = context_for(FakeToolbox(flights=FakeFlights([])))

    await _search_flights(
        context, {"origins": ["SFO"], "destinations": ["HND"], "departure_date": DEPART.isoformat()}
    )

    assert "flights" not in context.pending_decisions


async def test_the_briefs_red_eye_answer_reaches_the_ranking():
    """The intake quick-pick must move the ordering, not just sit in the brief.

    The base ranking used to be handed a blank FlightPreferences(), so a trip
    that said "avoid red-eyes" ranked exactly like one that never answered -
    a stated preference that moved nothing, the ledger's oldest failure.
    """
    offers = [
        offer("off_red", "SFO", 560, 0, depart_hour=1),
        offer("off_day", "SFO", 600, 0, depart_hour=11),
    ]

    # Nobody answered: the cheaper red-eye wins on price.
    silent = context_for(FakeToolbox(flights=FakeFlights(offers)))
    assert silent.state.brief.avoid_red_eye is None
    reply = await _search_flights(
        silent, {"origins": ["SFO"], "destinations": ["HND"], "departure_date": DEPART.isoformat()}
    )
    assert reply["offers"][0]["offer_ref"] == "off_red"

    # The trip said avoid: forty dollars does not outbid the answer.
    averse_state = sample_state()
    averse_state.brief.avoid_red_eye = True
    averse = context_for(FakeToolbox(flights=FakeFlights(offers)), state=averse_state)
    reply = await _search_flights(
        averse, {"origins": ["SFO"], "destinations": ["HND"], "departure_date": DEPART.isoformat()}
    )
    assert reply["offers"][0]["offer_ref"] == "off_day"


# --- airports ----------------------------------------------------------------


async def test_an_airport_search_with_a_role_stages_that_decision():
    context = context_for(
        FakeToolbox(airports=FakeAirports([airport("SFO", 18.0), airport("SJC", 61.0)]))
    )

    await _search_airports(context, {"location": "San Francisco Bay Area", "role": "departure"})

    assert "departure_airport" in context.pending_decisions
    decision = context.pending_decisions["departure_airport"]
    assert [option["data"]["iata"] for option in decision["options"]] == ["SFO", "SJC"]
    assert "18 min drive" in decision["options"][0]["pros"][0]


async def test_an_unmeasured_drive_is_named_as_a_gap_not_scored_as_a_zero():
    """A straight-line distance is not a drive, and silence is not zero minutes."""
    context = context_for(
        FakeToolbox(airports=FakeAirports([airport("OAK", None, source="unavailable")]))
    )

    await _search_airports(context, {"location": "Oakland", "role": "departure"})

    option = context.pending_decisions["departure_airport"]["options"][0]
    assert option["cons"] == [
        "drive time was not measured, so this airport cannot be compared on access"
    ]
    assert not any("drive" in pro for pro in option["pros"])


# --- booked ------------------------------------------------------------------


class ExplodingFlights:
    """A provider that must not be reached."""

    async def search_flights(self, spec):
        raise AssertionError("searched for flights that are already booked")


def booked_flights_state():
    state = sample_state()
    state.decisions.flights = Decision[FlightOptionData](
        status="selected",
        booked=True,
        booked_reference="ABC123",
        options=[
            DecisionOption[FlightOptionData](
                option_id="opt_chosen", data=offer("off_a", "SFO", 742, 0), status="selected"
            )
        ],
        selected_option_id="opt_chosen",
    )
    return state


async def test_booked_flights_are_not_searched_again():
    """Checked before the provider call: a booked trip should not spend quota
    re-pricing a decision nobody is going to change."""
    context = context_for(FakeToolbox(flights=ExplodingFlights()), booked_flights_state())

    reply = await _search_flights(
        context, {"origins": ["SFO"], "destinations": ["HND"], "departure_date": DEPART.isoformat()}
    )

    assert reply["already_booked"] is True
    assert "ABC123" in reply["message"]
    assert "flights" not in context.pending_decisions


async def test_the_traveller_can_still_ask_to_look():
    """`booked` is the traveller's own fact, so only they can wave it aside."""
    offers = [offer("off_a", "SFO", 742, 0)]
    context = context_for(FakeToolbox(flights=FakeFlights(offers)), booked_flights_state())

    reply = await _search_flights(
        context,
        {
            "origins": ["SFO"],
            "destinations": ["HND"],
            "departure_date": DEPART.isoformat(),
            "override_booked": True,
        },
    )

    assert "already_booked" not in reply
    assert reply["offers"]


async def test_the_model_is_told_which_decisions_are_booked():
    """A rule the model cannot see the state for is not a rule."""
    from app.agent.context import summarize

    summary = summarize(booked_flights_state())

    flights = next(row for row in summary["decisions"] if row["name"] == "flights")
    assert flights["booked"] is True
    assert flights["selected"] == "SFO → HND · UA · 11:00"


async def test_without_a_role_no_airport_decision_is_invented():
    """We cannot say which end of the trip it belongs to, so we file nothing."""
    context = context_for(FakeToolbox(airports=FakeAirports([airport("SFO", 18.0)])))

    reply = await _search_airports(context, {"location": "San Francisco Bay Area"})

    assert reply["airports"], "the answer is still returned to the model"
    assert context.pending_decisions == {}
