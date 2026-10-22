"""Idempotency-Key behaviour, including the concurrent-duplicate case."""

import asyncio
import uuid

from .conftest import auth, idem, make_event, make_users


async def test_same_key_twice_does_the_work_once(client, user_token, pool):
    event = await make_event(pool)
    seats = event["seat_ids"][:2]
    body = {"event_id": event["id"], "seat_ids": seats}
    headers = {**auth(user_token), **idem("stable-key-1")}

    first = await client.post("/api/holds", json=body, headers=headers)
    second = await client.post("/api/holds", json=body, headers=headers)

    assert first.status_code == second.status_code == 201
    # Deep equality, not string equality: response_body is jsonb, and Postgres
    # does not preserve JSON key order. Key order carries no meaning.
    assert first.json() == second.json()
    assert first.json()["hold_id"] == second.json()["hold_id"]

    # ONE hold, not two -- the replay did no work.
    assert await pool.fetchval("SELECT count(*) FROM holds") == 1
    assert await pool.fetchval("SELECT count(*) FROM seats WHERE status='held'") == 2


async def test_key_order_in_the_body_does_not_matter(client, user_token, pool):
    """The request hash is canonical (sorted keys), so a client that serialises
    its JSON in a different order is still recognised as the same request."""
    event = await make_event(pool)
    seats = event["seat_ids"][:1]
    headers = {**auth(user_token), **idem("stable-key-2")}

    a = await client.post("/api/holds",
                          json={"event_id": event["id"], "seat_ids": seats},
                          headers=headers)
    b = await client.post("/api/holds",
                          json={"seat_ids": seats, "event_id": event["id"]},
                          headers=headers)
    assert a.status_code == b.status_code == 201
    assert a.json() == b.json()


async def test_same_key_different_body_is_422(client, user_token, pool):
    event = await make_event(pool)
    headers = {**auth(user_token), **idem("stable-key-3")}

    first = await client.post(
        "/api/holds",
        json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
        headers=headers)
    assert first.status_code == 201

    clash = await client.post(
        "/api/holds",
        json={"event_id": event["id"], "seat_ids": [event["seat_ids"][1]]},
        headers=headers)
    assert clash.status_code == 422
    assert clash.json()["error"]["code"] == "idempotency_mismatch"
    # The mismatched request did nothing: still one hold, still one held seat.
    assert await pool.fetchval("SELECT count(*) FROM holds") == 1


async def test_key_is_scoped_per_user(client, pool):
    """The primary key is (user_id, key), so two users may legitimately pick
    the same UUID without colliding."""
    event = await make_event(pool, rows=1, cols=2)
    t1, t2 = await make_users(pool, 2)
    shared_key = idem("the-same-key")

    a = await client.post("/api/holds",
                          json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                          headers={**auth(t1), **shared_key})
    b = await client.post("/api/holds",
                          json={"event_id": event["id"], "seat_ids": [event["seat_ids"][1]]},
                          headers={**auth(t2), **shared_key})
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["hold_id"] != b.json()["hold_id"]
    assert await pool.fetchval("SELECT count(*) FROM holds") == 2


async def test_concurrent_duplicates_of_one_key(client, user_token, pool):
    """Ten simultaneous copies of the SAME request with the SAME key.

    This is the double-click that Nginx round-robins to three replicas at once.
    Exactly one INSERT into idempotency_keys wins; the other nine block on the
    primary key, then read and replay the committed response. Net effect: one
    hold, ten identical 201s.
    """
    event = await make_event(pool)
    body = {"event_id": event["id"], "seat_ids": event["seat_ids"][:2]}
    headers = {**auth(user_token), **idem("race-key")}

    responses = await asyncio.gather(*(
        client.post("/api/holds", json=body, headers=headers) for _ in range(10)
    ))

    assert all(r.status_code == 201 for r in responses), \
        [r.status_code for r in responses]
    hold_ids = {r.json()["hold_id"] for r in responses}
    assert len(hold_ids) == 1, "all ten callers must get the SAME hold"
    assert await pool.fetchval("SELECT count(*) FROM holds") == 1
    assert await pool.fetchval("SELECT count(*) FROM idempotency_keys") == 1


async def test_a_failed_request_frees_its_key_for_retry(client, pool):
    """A rejected request rolls back its idempotency row, so the client may
    retry the SAME action with the SAME key once the cause is gone.

    Without this, losing a seat race would permanently burn the key and the
    user's retry would replay a 409 forever.
    """
    event = await make_event(pool, rows=1, cols=1)
    seat = event["seat_ids"][0]
    t1, t2 = await make_users(pool, 2)
    key = idem("retry-key")

    assert (await client.post("/api/holds",
                              json={"event_id": event["id"], "seat_ids": [seat]},
                              headers={**auth(t1), **idem("winner")})).status_code == 201

    lost = await client.post("/api/holds",
                             json={"event_id": event["id"], "seat_ids": [seat]},
                             headers={**auth(t2), **key})
    assert lost.status_code == 409
    # The loser's key row was rolled back with the rest of its transaction.
    # (The winner's key row legitimately survives -- it recorded a success.)
    assert await pool.fetchval(
        "SELECT count(*) FROM idempotency_keys WHERE key = 'retry-key'") == 0
    assert await pool.fetchval(
        "SELECT count(*) FROM idempotency_keys WHERE key = 'winner'") == 1

    # Seat is freed, the same key now works.
    await client.delete(f"/api/holds/{(await pool.fetchval('SELECT id FROM holds'))}",
                        headers=auth(t1))
    retry = await client.post("/api/holds",
                              json={"event_id": event["id"], "seat_ids": [seat]},
                              headers={**auth(t2), **key})
    assert retry.status_code == 201


async def test_confirm_requires_a_key(client, user_token, pool):
    event = await make_event(pool)
    hold = await client.post("/api/holds",
                             json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                             headers={**auth(user_token), **idem(str(uuid.uuid4()))})
    r = await client.post(f"/api/holds/{hold.json()['hold_id']}/confirm",
                          headers=auth(user_token))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "idempotency_key_required"


async def test_keys_do_not_cross_endpoints(client, user_token, pool):
    """One key used on /api/holds and then on /confirm is a client bug, and it
    must not replay the hold's response as if it were a payment."""
    event = await make_event(pool)
    shared = idem("cross-endpoint-key")
    hold = await client.post("/api/holds",
                             json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                             headers={**auth(user_token), **shared})
    assert hold.status_code == 201

    r = await client.post(f"/api/holds/{hold.json()['hold_id']}/confirm",
                          headers={**auth(user_token), **shared})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "idempotency_mismatch"
