"""The web UI and the one endpoint it needs beyond the plain trip API."""

import re

from tests.conftest import sample_state


async def test_the_ui_is_served_from_the_app(client):
    response = await client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "<title>Travel Agent</title>" in body
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
