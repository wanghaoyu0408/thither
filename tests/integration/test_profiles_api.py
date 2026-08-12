

async def test_a_taken_profile_id_is_refused_rather_than_a_500(client):
    """It used to reach the database as an unhandled IntegrityError and come
    back as a bare `500 Internal Server Error` with an empty body.

    `scripts/demo_milestone1.py` posts a fixed profile id, so its very first
    call crashed on every run after the first - the open-source onboarding
    path, failing for anyone who ran it twice.
    """
    body = {"profile_id": "user_taken", "name": "Sam"}

    first = await client.post("/profiles", json=body)
    assert first.status_code == 201

    second = await client.post("/profiles", json=body)
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]
    assert "PATCH" in second.json()["detail"], "say what to do instead"
