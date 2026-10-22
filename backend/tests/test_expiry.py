"""Lazy hold expiry: no background job, and still correct."""

import asyncio
import uuid

import pytest

from app import config

from .conftest import auth, idem, make_event, make_users


def key() -> dict:
    return idem(str(uuid.uuid4()))


@pytest.fixture
def short_holds(monkeypatch):
    """Holds live for 1 second in these tests.

    holds.py reads config.HOLD_SECONDS at call time, so monkeypatching the
    module attribute is enough -- no restart, no env juggling.
    """
    monkeypatch.setattr(config, "HOLD_SECONDS", 1)
    return 1


async def test_expired_hold_releases_its_seat_to_another_user(
        client, pool, short_holds):
    """The core claim: a seat held by a hold that aged out is claimable again,
    even though NOTHING ran to clean it up."""
    event = await make_event(pool, rows=1, cols=1)
    seat = event["seat_ids"][0]
    t1, t2 = await make_users(pool, 2)

    first = await client.post("/api/holds",
                              json={"event_id": event["id"], "seat_ids": [seat]},
                              headers={**auth(t1), **key()})
    assert first.status_code == 201

    # Immediately contested -> loses, as it should.
    blocked = await client.post("/api/holds",
                                json={"event_id": event["id"], "seat_ids": [seat]},
                                headers={**auth(t2), **key()})
    assert blocked.status_code == 409

    await asyncio.sleep(1.3)

    # The seats row STILL says 'held' and still points at the old hold --
    # nothing swept it. Lazy expiry is purely a matter of how we read.
    raw = await pool.fetchrow("SELECT status, hold_id FROM seats WHERE id = $1", seat)
    assert raw["status"] == "held"
    assert raw["hold_id"] is not None

    # ...but every read path and the claim itself treat it as free.
    assert (await client.get(f"/api/events/{event['id']}/seats")
            ).json()["seats"][0]["status"] == "available"

    won = await client.post("/api/holds",
                            json={"event_id": event["id"], "seat_ids": [seat]},
                            headers={**auth(t2), **key()})
    assert won.status_code == 201
    assert won.json()["hold_id"] != first.json()["hold_id"]

    # The seat now points at the NEW hold.
    assert await pool.fetchval("SELECT hold_id FROM seats WHERE id = $1", seat) \
        == uuid.UUID(won.json()["hold_id"])


async def test_confirming_an_expired_hold_is_410(client, user_token, pool, short_holds):
    event = await make_event(pool)
    hold = await client.post("/api/holds",
                             json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                             headers={**auth(user_token), **key()})
    hold_id = hold.json()["hold_id"]

    await asyncio.sleep(1.3)

    r = await client.post(f"/api/holds/{hold_id}/confirm",
                          headers={**auth(user_token), **key()})
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "hold_expired"
    assert await pool.fetchval("SELECT count(*) FROM bookings") == 0


async def test_hold_detail_reports_expired(client, user_token, pool, short_holds):
    event = await make_event(pool)
    hold = await client.post("/api/holds",
                             json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                             headers={**auth(user_token), **key()})
    hold_id = hold.json()["hold_id"]

    assert (await client.get(f"/api/holds/{hold_id}",
                             headers=auth(user_token))).json()["expired"] is False
    await asyncio.sleep(1.3)
    detail = (await client.get(f"/api/holds/{hold_id}", headers=auth(user_token))).json()
    # Still 'active' as a status; 'expired' is derived from the clock. The UI
    # uses `expired` to show "your hold timed out" with a way back.
    assert detail["expired"] is True
    assert detail["status"] == "active"


async def test_expired_hold_does_not_block_the_users_next_hold(
        client, user_token, pool, short_holds):
    """The one-hold-per-user-per-event rule must also respect expiry, or a user
    whose hold timed out would be locked out of the event."""
    event = await make_event(pool, rows=1, cols=2)
    first = await client.post("/api/holds",
                              json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                              headers={**auth(user_token), **key()})
    assert first.status_code == 201

    blocked = await client.post("/api/holds",
                                json={"event_id": event["id"], "seat_ids": [event["seat_ids"][1]]},
                                headers={**auth(user_token), **key()})
    assert blocked.status_code == 409    # hold_limit, correctly

    await asyncio.sleep(1.3)
    allowed = await client.post("/api/holds",
                                json={"event_id": event["id"], "seat_ids": [event["seat_ids"][1]]},
                                headers={**auth(user_token), **key()})
    assert allowed.status_code == 201


async def test_expired_holds_are_counted_in_admin_stats(
        client, user_token, admin_token, pool, short_holds):
    """Honest bookkeeping: the admin page shows how many stale hold rows the
    no-sweeper trade-off has accumulated."""
    event = await make_event(pool, rows=1, cols=3)
    await client.post("/api/holds",
                      json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                      headers={**auth(user_token), **key()})
    await asyncio.sleep(1.3)

    stats = (await client.get(f"/api/admin/events/{event['id']}/stats",
                              headers=auth(admin_token))).json()
    assert stats["expired_holds"] == 1
    assert stats["held"] == 0          # not held any more, as far as users see
    assert stats["available"] == 3
    assert stats["integrity_ok"] is True
