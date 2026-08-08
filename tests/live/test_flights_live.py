"""Duffel contract, opt-in.

    .\\.venv\\Scripts\\python.exe -m pytest -m live --override-ini addopts=

This is an *integration* acceptance and nothing more. Duffel's own docs say test
mode "won't see realistic flight schedules or prices", so every assertion here
is about the contract - the request is accepted, offers parse, stops and
durations come out sane, and sandbox data is labelled as such. Nothing asserts
that a fare is reasonable, because in the sandbox it is not.

The recommendation-quality acceptance waits for a live token.
"""

from datetime import date, timedelta

import pytest

from app.config import get_settings
from app.models.flight import SearchAirportsInput, SearchFlightsInput
from app.services.flight_service import SANDBOX_DISCLAIMER
from app.services.toolbox import Toolbox

settings = get_settings()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.duffel_access_token, reason="needs DUFFEL_ACCESS_TOKEN"),
]

DEPART = date.today() + timedelta(days=45)


@pytest.fixture
async def toolbox():
    async with Toolbox(settings) as box:
        yield box


def skip_if_no_search_permission(result) -> None:
    """A Duffel token can be valid and still not be allowed to search.

    That is a dashboard setting rather than a defect here, so it skips with the
    remedy named - but it skips loudly enough that nobody reads the milestone as
    verified when it was not.
    """
    if result.ok:
        return
    if result.error.code == "auth_failed" and "permission" in result.error.message.lower():
        pytest.skip(
            "the Duffel token lacks the 'air.offer_requests.create' permission; "
            "enable it on the token in the Duffel dashboard to run this"
        )


async def test_an_offer_request_is_accepted_and_parses(toolbox):
    result = await toolbox.flights.search_flights(
        SearchFlightsInput(origins=["SFO"], destinations=["LHR"], departure_date=DEPART, adults=1)
    )

    skip_if_no_search_permission(result)
    assert result.ok, result.error
    if result.found_nothing:
        pytest.skip("the sandbox had no inventory for this route today")

    for option in result.results:
        assert option.offer_ref
        assert option.price.amount > 0
        assert option.origin and option.destination
        assert option.slices, "an offer with no slice should have been dropped"
        assert option.stops is not None and option.stops >= 0
        if option.duration_minutes is not None:
            assert 0 < option.duration_minutes < 3 * 24 * 60


async def test_sandbox_offers_are_labelled_and_disclaimed(toolbox):
    """The whole point of the flag: a test fare must never read as a real one."""
    if toolbox.flights.live_mode:
        pytest.skip("this token is live; the sandbox labelling test does not apply")

    result = await toolbox.flights.search_flights(
        SearchFlightsInput(origins=["SFO"], destinations=["LHR"], departure_date=DEPART, adults=1)
    )
    skip_if_no_search_permission(result)
    assert result.ok, result.error
    if result.found_nothing:
        pytest.skip("the sandbox had no inventory for this route today")

    assert all(option.live_mode is False for option in result.results)
    assert any(SANDBOX_DISCLAIMER in warning for warning in result.warnings)


async def test_several_origins_fan_out_into_one_ranked_list(toolbox):
    result = await toolbox.flights.search_flights(
        SearchFlightsInput(
            origins=["SFO", "OAK", "SJC"],
            destinations=["LHR"],
            departure_date=DEPART,
            adults=1,
        )
    )

    skip_if_no_search_permission(result)
    assert result.ok, result.error
    # Whichever origins the provider serves, nothing outside the request appears.
    assert {option.origin for option in result.results} <= {"SFO", "OAK", "SJC"}


async def test_a_nonsense_route_fails_loudly_rather_than_silently(toolbox):
    result = await toolbox.flights.search_flights(
        SearchFlightsInput(origins=["ZZZ"], destinations=["QQQ"], departure_date=DEPART, adults=1)
    )

    skip_if_no_search_permission(result)

    # Either the provider rejects it or it simply has nothing. Both are fine and
    # distinguishable; inventing an itinerary would not be.
    assert result.found_nothing or not result.ok
    if not result.ok:
        assert result.error.code in ("invalid_request", "provider_unavailable")


async def test_airports_near_the_bay_come_back_with_real_drive_times(toolbox):
    """Needs the Google key rather than the Duffel one, but it is the same journey."""
    if not settings.google_maps_api_key:
        pytest.skip("needs GOOGLE_MAPS_API_KEY")

    result = await toolbox.airports.search_airports(
        SearchAirportsInput(location="San Francisco Bay Area", limit=4)
    )

    assert result.ok, result.error
    codes = {option.iata for option in result.results}
    assert {"SFO", "OAK", "SJC"} & codes, codes

    timed = [option for option in result.results if option.ground_travel_minutes is not None]
    assert timed, "no drive time came back from the Routes API"
    for option in timed:
        assert option.ground_travel_source == "routes_api"
        assert 0 < option.ground_travel_minutes < 300
