"""Auth: signup, login, and that the guards actually guard."""

import pytest

from .conftest import auth, make_event, idem


async def test_signup_then_login(client):
    r = await client.post("/api/auth/signup",
                          json={"email": "New.User@Example.com", "password": "hunter22"})
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["role"] == "user", "self-signup must never grant admin"
    # Email is normalised to lowercase, so Example.com and example.com are the
    # same account and cannot be registered twice.
    assert body["user"]["email"] == "new.user@example.com"
    assert body["token"]

    r = await client.post("/api/auth/login",
                          json={"email": "new.user@example.com", "password": "hunter22"})
    assert r.status_code == 200
    assert r.json()["token"]


async def test_signup_duplicate_email_is_409(client):
    payload = {"email": "dupe@example.com", "password": "hunter22"}
    assert (await client.post("/api/auth/signup", json=payload)).status_code == 201
    r = await client.post("/api/auth/signup", json=payload)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"


async def test_signup_rejects_bad_email_and_short_password(client):
    r = await client.post("/api/auth/signup",
                          json={"email": "not-an-email", "password": "hunter22"})
    assert r.status_code == 422
    r = await client.post("/api/auth/signup",
                          json={"email": "ok@example.com", "password": "123"})
    assert r.status_code == 422


@pytest.mark.parametrize("password", ["wrong-password", ""])
async def test_login_wrong_password(client, password):
    await client.post("/api/auth/signup",
                      json={"email": "pw@example.com", "password": "correct-pw"})
    r = await client.post("/api/auth/login",
                          json={"email": "pw@example.com", "password": password})
    assert r.status_code in (401, 422)
    if r.status_code == 401:
        assert r.json()["error"]["code"] == "invalid_credentials"


async def test_login_unknown_email_gives_same_error_as_wrong_password(client):
    """No account enumeration: both failures must look identical to the client."""
    await client.post("/api/auth/signup",
                      json={"email": "known@example.com", "password": "correct-pw"})
    wrong_pw = await client.post("/api/auth/login",
                                 json={"email": "known@example.com", "password": "nope-nope"})
    no_user = await client.post("/api/auth/login",
                                json={"email": "ghost@example.com", "password": "nope-nope"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()


async def test_me_requires_a_token(client):
    assert (await client.get("/api/auth/me")).status_code == 401
    r = await client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


async def test_me_returns_the_caller(client, user_token):
    r = await client.get("/api/auth/me", headers=auth(user_token))
    assert r.status_code == 200
    assert r.json()["email"] == "user@test.local"


async def test_protected_write_route_needs_a_token(client, pool):
    event = await make_event(pool)
    r = await client.post("/api/holds",
                          json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                          headers=idem("k1"))
    assert r.status_code == 401


@pytest.mark.parametrize("path", [
    "/api/admin/events/1/stats",
    "/api/ops/cluster",
    "/api/ops/logs",
])
async def test_admin_routes_reject_a_plain_user(client, user_token, path):
    """A valid token is not enough -- the `role` claim is checked too."""
    r = await client.get(path, headers=auth(user_token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


async def test_admin_can_create_an_event_and_seats_are_generated(client, admin_token):
    r = await client.post(
        "/api/admin/events",
        headers=auth(admin_token),
        json={
            "title": "Admin Made", "venue": "Hall",
            "starts_at": "2027-01-01T18:00:00Z",
            "sale_opens_at": "2026-01-01T18:00:00Z",
            "rows": 3, "cols": 4, "price_cents": 5000,
        },
    )
    assert r.status_code == 201
    assert r.json()["seats_created"] == 12

    seats = (await client.get(f"/api/events/{r.json()['id']}/seats")).json()["seats"]
    assert len(seats) == 12
    # Row letters and 1-based column numbers.
    assert seats[0]["label"] == "A1"
    assert seats[-1]["label"] == "C4"


async def test_event_create_requires_a_timezone(client, admin_token):
    """A naive datetime would be silently treated as UTC and the countdown
    would be wrong by the user's offset."""
    r = await client.post(
        "/api/admin/events",
        headers=auth(admin_token),
        json={"title": "No TZ", "venue": "Hall",
              "starts_at": "2027-01-01T18:00:00",
              "sale_opens_at": "2026-01-01T18:00:00",
              "rows": 2, "cols": 2, "price_cents": 100},
    )
    assert r.status_code == 422


async def test_server_time_endpoint(client):
    r = await client.get("/api/time")
    assert r.status_code == 200
    assert "epoch_ms" in r.json()
