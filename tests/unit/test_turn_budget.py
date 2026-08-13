"""What one turn is allowed to spend, and where that is enforced.

The point of these tests is *where* the stop happens. A budget that refuses
after the request has gone out has cost the same money as no budget at all, so
most of what follows is an assertion about the network never being reached -
not about a return value.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.models.place import PlaceFieldSet, SearchPlacesInput
from app.providers import base
from app.providers.base import (
    BudgetExhausted,
    ProviderUnavailable,
    TurnBudget,
    budget_for,
    current_budget,
    request_json,
    turn_budget,
)
from app.providers.google_places import PROVIDER, GooglePlacesProvider
from app.services.cache import InProcessCache, LayeredCache
from app.services.place_service import PlaceService

SHIBUYA = {"lat": 35.6595, "lng": 139.7005}

PLACES_PAYLOAD = {
    "places": [
        {
            "id": "ChIJ_a",
            "displayName": {"text": "Fuglen Tokyo"},
            "location": {"latitude": 35.6659, "longitude": 139.6979},
            "rating": 4.3,
        }
    ]
}


def counting_client(
    payload: dict | None = None, status: int = 200
) -> tuple[httpx.AsyncClient, list]:
    """A client that records every request it is actually asked to make."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload if payload is not None else {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def get(client: httpx.AsyncClient, provider: str = "google_places") -> dict:
    return await request_json(client, "GET", "https://example.test/x", provider=provider)


# --- the meter itself --------------------------------------------------------


def test_an_unlisted_provider_is_bounded_by_default_not_exempt_by_default():
    """A provider added later must not be free until someone remembers it."""
    budget = TurnBudget(limits={"google_places": 3}, default=2)

    assert budget.limit("google_places") == 3
    assert budget.limit("a_provider_nobody_has_written_yet") == 2


def test_spending_counts_per_provider_not_in_one_pot():
    budget = TurnBudget(limits={"duffel": 2, "google_places": 2})
    budget.spend("duffel")
    budget.spend("duffel")

    with pytest.raises(BudgetExhausted):
        budget.spend("duffel")

    # Flights being spent must not stop places being searched.
    budget.spend("google_places")
    assert budget.as_log() == {"duffel": 2, "google_places": 1}


def test_the_refusal_says_we_stopped_rather_than_they_did():
    """`rate_limited` would blame the provider and invite a doomed retry."""
    budget = TurnBudget(limits={"duffel": 0})

    with pytest.raises(BudgetExhausted) as excinfo:
        budget.spend("duffel")

    error = excinfo.value.as_tool_error()
    assert error.code == "budget_exhausted"
    assert error.retryable is False
    assert error.provider == "duffel"


# --- where the stop happens --------------------------------------------------


async def test_an_exhausted_budget_stops_the_call_before_the_network():
    client, seen = counting_client()

    with turn_budget(TurnBudget(limits={"google_places": 1})):
        await get(client)
        with pytest.raises(BudgetExhausted):
            await get(client)

    # One request went out, not two. A guard that refuses after the request has
    # already been billed is decoration.
    assert len(seen) == 1


async def test_retries_are_charged_once(monkeypatch):
    """A flaky provider must not eat a turn's whole budget on one call."""
    monkeypatch.setattr(base, "BACKOFF_SECONDS", 0)
    client, seen = counting_client(status=503)

    budget = TurnBudget(limits={"google_places": 5})
    with turn_budget(budget):
        with pytest.raises(ProviderUnavailable):
            await get(client)

    assert len(seen) == base.MAX_ATTEMPTS  # three attempts over the wire
    assert budget.as_log() == {"google_places": 1}  # one logical call charged


async def test_without_a_budget_nothing_is_limited():
    """Scripts and tests are not turns. Metering belongs to the agent loop."""
    client, seen = counting_client()

    assert current_budget() is None
    for _ in range(30):
        await get(client)

    assert len(seen) == 30


async def test_each_task_counts_its_own_turn():
    """Two travellers at once must not spend each other's budget."""
    client, _ = counting_client()

    async def one_turn(calls: int) -> dict[str, int]:
        budget = TurnBudget(limits={"google_places": 10})
        with turn_budget(budget):
            for _ in range(calls):
                await get(client)
                await asyncio.sleep(0)  # yield, so the tasks interleave
        return budget.as_log()

    first, second = await asyncio.gather(one_turn(3), one_turn(5))

    assert first == {"google_places": 3}
    assert second == {"google_places": 5}


# --- what a caller sees ------------------------------------------------------


def place_service(client: httpx.AsyncClient) -> PlaceService:
    return PlaceService(
        GooglePlacesProvider("test-key", client), LayeredCache(InProcessCache(), None)
    )


async def test_exhaustion_degrades_like_an_outage_and_invents_nothing():
    """Saving money must not become a reason to make a figure up."""
    client, seen = counting_client(PLACES_PAYLOAD)
    service = place_service(client)

    with turn_budget(TurnBudget(limits={PROVIDER: 0})):
        result = await service.search_places(SearchPlacesInput(query="ramen", **SHIBUYA))

    assert result.ok is False
    assert result.error.code == "budget_exhausted"
    assert result.error.retryable is False
    assert result.results == []
    assert seen == []


async def test_a_cache_hit_costs_nothing():
    """The budget counts money, not intent - and a cached answer is free."""
    client, seen = counting_client(PLACES_PAYLOAD)
    service = place_service(client)
    spec = SearchPlacesInput(query="ramen", **SHIBUYA, field_set=PlaceFieldSet.RANKING)

    budget = TurnBudget(limits={PROVIDER: 1})
    with turn_budget(budget):
        first = await service.search_places(spec)
        second = await service.search_places(spec)

    assert first.ok and second.ok
    assert [p.place_id for p in second.results] == ["ChIJ_a"]
    assert len(seen) == 1
    assert budget.as_log() == {PROVIDER: 1}


# --- the model call ----------------------------------------------------------


class RecordingOpenAI:
    """Stands in for AsyncOpenAI and keeps what it was handed."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.created: list[dict] = []
        outer = self

        class Responses:
            async def create(self, **create_kwargs):
                outer.created.append(create_kwargs)
                return outer.response

        self.responses = Responses()
        self.response = SimpleNamespace(output=[], usage=None, id="resp_1")


def openai_client(monkeypatch, settings=None):
    from app.config import Settings
    from app.providers import openai_llm

    recorder: dict = {}

    def build(**kwargs):
        client = RecordingOpenAI(**kwargs)
        recorder["client"] = client
        return client

    monkeypatch.setattr(openai_llm, "AsyncOpenAI", build)
    config = settings or Settings(database_url="sqlite+aiosqlite:///:memory:")
    return openai_llm.OpenAIClient("test-key", "gpt-5", settings=config), recorder


async def test_the_model_is_given_a_ceiling_on_what_it_writes(monkeypatch):
    """Without this, one confused reply can be as long as the model allows."""
    from app.config import Settings

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:", openai_max_output_tokens=1234
    )
    client, recorder = openai_client(monkeypatch, settings)

    await client.respond(instructions="hi", conversation=[], tools=[])

    created = recorder["client"].created[0]
    assert created["max_output_tokens"] == 1234


def test_the_reply_ceiling_leaves_room_for_reasoning():
    """`max_output_tokens` counts reasoning tokens, not just visible ones.

    A cap sized from visible output would cut a model off mid-thought, and a
    reply whose reasoning consumed the whole budget arrives empty. Measured
    worst case on this workload is 712 tokens at high reasoning effort, so the
    default has to clear that by a wide margin - and it costs nothing to, since
    a ceiling is not a reservation.
    """
    from app.config import Settings

    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")

    assert settings.openai_max_output_tokens >= 16_000


async def test_the_sdk_defaults_are_not_inherited(monkeypatch):
    """600s and two silent retries would let one hung call hold a turn."""
    _, recorder = openai_client(monkeypatch)

    kwargs = recorder["client"].kwargs
    assert kwargs["timeout"] == 120.0
    assert kwargs["max_retries"] == 1


async def test_a_reply_stopped_by_the_output_cap_is_marked(monkeypatch):
    """Capping output silently would hand back a fragment as an answer."""
    client, recorder = openai_client(monkeypatch)
    recorder["client"].response = SimpleNamespace(
        output=[],
        usage=None,
        id="resp_1",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )

    turn = await client.respond(instructions="hi", conversation=[], tools=[])

    assert turn.truncated is True


async def test_a_reply_that_finished_is_not_marked(monkeypatch):
    client, _ = openai_client(monkeypatch)

    turn = await client.respond(instructions="hi", conversation=[], tools=[])

    assert turn.truncated is False


async def test_research_is_metered_even_though_it_skips_request_json(monkeypatch):
    """It speaks to the SDK, so the choke point in `request_json` never sees it.

    Its own tool budget is not enough either: `recommend_hotel_areas` reaches
    this provider without going through `research_web`.
    """
    from app.config import Settings
    from app.models.research import ResearchWebInput
    from app.providers import openai_research

    built: dict = {}
    monkeypatch.setattr(
        openai_research,
        "AsyncOpenAI",
        lambda **kwargs: built.setdefault("client", RecordingOpenAI(**kwargs)),
    )
    provider = openai_research.OpenAIResearchProvider(
        "test-key", "gpt-5", settings=Settings(database_url="sqlite+aiosqlite:///:memory:")
    )

    with turn_budget(TurnBudget(limits={openai_research.PROVIDER: 0})):
        result = await provider.research(ResearchWebInput(query="ramen in Shibuya"))

    assert result.ok is False
    assert result.error.code == "budget_exhausted"
    assert built["client"].created == []  # the model was never called


# --- the ceilings themselves -------------------------------------------------


async def test_a_toolbox_opened_outside_a_turn_still_meters(monkeypatch):
    """Five HTTP endpoints build their own Toolbox. None of them is a turn.

    A button press is bounded by the size of the trip rather than by a model's
    judgement, so it is not the runaway risk - but "paid calls are counted" has
    to be true without an asterisk, and the sixth endpoint to be written will
    not remember to ask.
    """
    from app.config import Settings
    from app.services.toolbox import Toolbox

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:", google_maps_api_key="test-key"
    )
    async with Toolbox(settings) as toolbox:
        assert current_budget() is not None
        assert toolbox.places is not None

    assert current_budget() is None


async def test_a_toolbox_inside_a_turn_does_not_start_a_second_count():
    """The agent's budget spans the turn; its Toolbox must not reset it."""
    from app.config import Settings
    from app.services.toolbox import Toolbox

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:", google_maps_api_key="test-key"
    )
    outer = TurnBudget(limits={"google_places": 5})
    outer.spend("google_places")

    with turn_budget(outer):
        async with Toolbox(settings):
            assert current_budget() is outer
            assert current_budget().as_log() == {"google_places": 1}


def test_every_provider_the_codebase_speaks_to_has_a_ceiling():
    """A provider whose name is missing here silently falls back to `default`.

    That is the safe direction, but it is worth knowing when it happens: the
    names are the ones passed to `request_json`, and they are easy to mistype.
    """
    from app.config import Settings

    budget = budget_for(Settings(database_url="sqlite+aiosqlite:///:memory:"))
    named = {
        "google_places",
        "google_routes",
        "google_weather",
        "open-meteo",
        "duffel",
        "serpapi_google_hotels",
    }

    assert named <= set(budget.limits)
    assert all(limit > 0 for limit in budget.limits.values())
    assert budget.default > 0
