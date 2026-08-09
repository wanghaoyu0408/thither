"""The web UI and the one endpoint it needs beyond the plain trip API."""

import re

from tests.conftest import sample_state


async def test_the_ui_is_served_from_the_app(client):
    response = await client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "<title>Travel Agent</title>" in body
    # Self-contained: nothing fetched from a host this project cannot vouch for.
    assert "//cdn." not in body
    assert "https://" not in body.split("<style>")[1].split("</style>")[0]


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
