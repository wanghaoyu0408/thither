"""The web UI and the one endpoint it needs beyond the plain trip API."""

import re

from tests.conftest import sample_state


async def test_the_ui_is_served_from_the_app(client):
    response = await client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "<title>问津 · Whither</title>" in body
    # No CDN for the app itself: markup, styles and script are all served here.
    assert "//cdn." not in body
    assert "https://" not in body.split("<style>")[1].split("</style>")[0]
    # Google's Maps JavaScript is the single exception, and it is loaded at
    # runtime only when a key exists - never as a <script src> in the document,
    # which would make the map a condition of the page rendering at all.
    external = set(re.findall(r'<script[^>]+src="(https?://[^"]+)"', body))
    assert external == set(), f"the document must not hard-load anything external: {external}"
    assert "maps.googleapis.com/maps/api/js" in body


async def test_the_page_carries_no_key_of_its_own(client):
    """The file on disk is committed to git and handed to whoever opens the
    app. A key the browser loads is public by construction, but that is not a
    reason to bake one into the source."""
    body = (await client.get("/")).text

    assert "AIza" not in body, "a Google API key looks committed into the page"
    assert "/ui-config" in body, "the page should fetch its key at runtime instead"


async def test_ui_config_serves_only_the_browser_key_field(client):
    """The page's config carries the browser key and nothing else."""
    response = await client.get("/ui-config")

    assert response.status_code == 200
    assert set(response.json()) == {"maps_key"}


def test_the_server_key_is_never_the_browser_key():
    """The fallback that once published the server key to the page is gone.

    A page publishes every key it loads, and the server key spends the Places
    and Routes budget. With only a server key configured the map goes dark -
    a smaller loss than a scraped quota.
    """
    from app.config import Settings

    server_only = Settings(google_maps_api_key="server-key", maps_browser_api_key=None)
    assert server_only.maps_browser_key is None

    split = Settings(google_maps_api_key="server-key", maps_browser_api_key="browser-key")
    assert split.maps_browser_key == "browser-key"

    none = Settings(google_maps_api_key=None, maps_browser_api_key=None)
    assert none.maps_browser_key is None


async def test_the_post_helper_takes_the_body_first(client):
    """Regression: `post` was declared `(path, body)` while every call site
    passes only the body, so every POST the UI made carried an empty `{}`.
    Move failed outright, and replan quietly ignored the requested pace - the
    failure mode of a dropped argument is a plausible-looking wrong answer."""
    body = await client.get("/")
    source = body.text

    assert "const post = (body) =>" in source, "post must take the body as its only argument"
    calls = re.findall(r"\bpost\(", source)
    assert calls, "expected the UI to use the post helper"


async def test_the_page_names_the_travel_dna_surface_and_the_new_tools(client):
    """The learning layer's whole UI ships in the one file, like everything else.

    A grep is all this can be, and a grep is not much: this test passed for
    weeks while the panel was unreachable, because a string in a template
    literal is present whether or not any code path can render it. The test
    that actually holds the feature up is
    `test_that_traveller_can_carry_learning` in test_trip_creation.py, which
    goes through the real create payload and asserts the surfaces can appear.
    """
    body = (await client.get("/")).text

    assert "Travel DNA" in body
    assert "Add to my travel profile" in body      # the consent card's yes
    assert "Not really" in body                    # and its durable no
    assert "How did this trip go?" in body         # the reflection card
    assert "record_stated_preference" in body      # TOOL_LABELS knows both tools
    assert "review_learned_preferences" in body
    # Ordinal words, never percentages: the badge tiers are the only vocabulary.
    for word in ("emerging", "likely", "strong"):
        assert f"dna-badge.{word}" in body or word in body


async def test_the_page_can_show_how_close_our_own_numbers_run(client):
    """The panel that exists to be mostly empty.

    A grep again, and worth as little as the last one: what holds this feature
    up is `tests/scenarios/test_milestone10_acceptance.py`, which goes through
    the real endpoints and asserts a never-checked dimension says so out loud
    rather than rendering nothing.
    """
    body = (await client.get("/")).text

    assert "How close my numbers run" in body
    assert "never checked against what actually happened" in body
    assert 'data-open="accuracy"' in body
    # The chip toggles a flag, and a hardcoded list decides which flags get
    # rendered. A kind missing from that list is a button that opens nothing -
    # which is exactly what happened, with every string above already present.
    rendered = re.search(r'for \(const kind of \[([^\]]+)\]\)', body, re.S)
    assert rendered and '"accuracy"' in rendered.group(1)
    # And the reflection can mark one of our estimates.
    assert "estimateQuestionsHtml" in body
    assert "data-estchip=" in body
    # Answers live in S, not the DOM: pressing a chip re-renders the workspace.
    assert "S.estimates" in body
    assert "estimates: estimateAnswers()" in body


async def test_the_new_trip_form_asks_who_the_trip_is_for(client):
    """Without a traveller the trip has nobody in it, and everything keyed to a
    person is unreachable rather than merely empty. The form collected a head
    count and never a person."""
    body = (await client.get("/")).text

    assert 'id="travelerPick"' in body
    assert 'id="travelerName"' in body
    assert "fillTravellers" in body
    # And the payload carries them, which is the part that was missing.
    assert 'travelers: [{ name: travellerName, role: "organizer", profile_id: profileId }]' in body


async def test_the_choice_cards_show_their_figures(client):
    """`OptionView` has `metrics`; the chooser card read `o.figures`.

    No such field, so every card ever rendered showed a name and its pros and
    cons and not one number - which is why the agent had to copy the whole
    ranking into its reply, and why choosing felt like a guess.
    """
    body = (await client.get("/")).text

    assert "figuresHtml(o.metrics)" in body
    assert "figuresHtml(o.figures)" not in body


async def test_the_brief_comes_before_the_questions(client):
    """Read it the way the traveller works: what they have already told us
    first, what is still being asked below it, the button that starts the trip
    last. The questions used to sit on top, pushing the very thing being
    confirmed below the fold."""
    body = (await client.get("/")).text

    assert body.index("briefCardHtml(view, { confirmed: false })") < body.index("Still asking")
    assert body.index("Still asking") < body.index("finalStepHtml(view)")


async def test_the_page_asks_whether_there_is_anything_else(client):
    body = (await client.get("/")).text

    assert "Anything else?" in body
    assert "finalStepHtml" in body
    # The toggle and the typed text live in S, never the DOM: render() replaces
    # the workspace, and ticking any quick-pick chip calls it.
    assert "S.extra" in body


async def test_the_page_starts_planning_when_the_brief_is_confirmed(client):
    """The button is labelled "Start planning" and used to write four fields
    and stop - so eight of eight trips needed the traveller to type again."""
    body = (await client.get("/")).text

    assert "Start planning" in body      # the label the button makes a promise with
    assert "startPlanning" in body       # and the code that keeps it


async def test_the_page_can_answer_a_fork(client):
    """The agent reached a dead end, named the way out in prose, and left
    nothing to press. Every one of its choices is a button now."""
    body = (await client.get("/")).text

    assert "pendingProposals" in body
    assert "proposalHtml" in body
    assert 'data-answer=' in body
    # And a waiting fork holds back the auto-continue: planning is stuck behind
    # it by definition.
    assert "pendingProposals().length || pendingChoices().length" in body


async def test_the_page_carries_on_after_the_last_choice(client):
    """A selection used to be a state change with no consequence: the card
    vanished, the bar went quiet, and planning stopped - beside a reply
    promising to find flights once the traveller had chosen."""
    body = (await client.get("/")).text

    assert "continueIfSettled" in body
    assert "agentSteps" in body          # reads the server's next_steps
    assert "paintHint" in body           # and says what is next when idle


async def test_the_page_remembers_which_trip_you_had_open(client):
    """Closing a laptop is not a decision to start over.

    The page held the selected trip in memory only, so any reload landed on
    "no trip selected" with the drawer open - which is what a traveller sees
    after their machine sleeps and the tab is discarded.
    """
    body = (await client.get("/")).text

    assert "whither.trip" in body
    assert "localStorage" in body
    # A browser with storage disabled must not take the app down over a
    # convenience, and a deleted trip must fall back to the drawer.
    assert "rememberedTrip" in body and "resetWorkspace()" in body


async def test_the_overview_endpoint_derives_what_the_trip_cannot_store(client, session):
    """Validation and conflicts are computed at read time, so a client cannot
    get them by reading the trip - and should not reimplement either rule."""
    from app.db.repository import TripRepository

    stored = await TripRepository(session).create(sample_state())

    response = await client.get(f"/trips/{stored.trip_id}/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["trip"]["trip_id"] == stored.trip_id
    assert body["validation"]["status"] in ("ok", "warnings", "errors", "unvalidated", "stale")
    assert isinstance(body["conflicts"], list)
    assert isinstance(body["blocking"], list)


async def test_the_overview_of_a_missing_trip_is_a_404(client):
    response = await client.get("/trips/trip_nope/overview")

    assert response.status_code == 404
