"""Things the app must guarantee so that the Nginx config is safe.

These are not tests of Nginx. They are tests of the PROMISES the app makes that
nginx.conf relies on. If one of these breaks, the edge config silently becomes
wrong -- which is exactly the kind of bug that is invisible until production.
"""

import uuid

from .conftest import auth, idem, make_event


async def test_cacheable_endpoints_ignore_the_caller(client, user_token, pool):
    """Nginx's cache key does not include the Authorization header, so a
    response cached for one user is served to the next caller of that URL.
    These three endpoints must therefore return the SAME bytes with and
    without a token."""
    event = await make_event(pool)
    for path in ("/api/events", f"/api/events/{event['id']}",
                 f"/api/events/{event['id']}/seats"):
        anon = await client.get(path)
        authed = await client.get(path, headers=auth(user_token))
        assert anon.status_code == authed.status_code == 200
        assert anon.json() == authed.json(), f"{path} varies by caller -- NOT cacheable"


async def test_cacheable_endpoints_set_no_cookies(client, pool):
    """Nginx refuses to cache a response carrying Set-Cookie. We do not rely on
    that safety net, but we must not be accidentally depending on it either:
    this API is JWT-based and sets no cookies at all."""
    event = await make_event(pool)
    for path in ("/api/events", f"/api/events/{event['id']}",
                 f"/api/events/{event['id']}/seats"):
        assert "set-cookie" not in (await client.get(path)).headers


async def test_every_error_uses_the_same_envelope(client, user_token, pool):
    """Nginx's 429/413/503 pages are hand-written to match this shape, so the
    frontend has exactly one error parser. Drift here breaks the UI's error
    handling for edge errors."""
    event = await make_event(pool, sale_opens_in_seconds=600)
    failures = [
        await client.get("/api/events/999999"),
        await client.get("/api/auth/me"),
        await client.post("/api/holds",
                          json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                          headers={**auth(user_token), **idem(str(uuid.uuid4()))}),
        await client.post("/api/auth/signup", json={"email": "x", "password": "y"}),
    ]
    for r in failures:
        assert r.status_code >= 400
        body = r.json()
        assert set(body) == {"error"}, body
        assert "code" in body["error"] and "message" in body["error"]
        assert isinstance(body["error"]["code"], str)


async def test_api_prefix_is_never_stripped(client):
    """Every route lives under /api, which is what lets nginx.conf use
    `proxy_pass http://api_backend;` with NO trailing slash and no rewrite.
    A route mounted outside /api would be unreachable through the gateway."""
    from app.main import app
    paths = [r.path for r in app.routes if getattr(r, "path", "").startswith("/")]
    offenders = [p for p in paths
                 if not p.startswith("/api") and p not in ("/openapi.json",)]
    assert offenders == [], f"routes outside /api are unreachable via Nginx: {offenders}"
