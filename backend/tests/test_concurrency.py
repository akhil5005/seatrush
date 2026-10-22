"""The tests this whole project exists for.

Every assertion here is about what happens when many people want the same seat
at the same instant. If any of these fail, SeatRush oversells.
"""

import asyncio
import uuid

from .conftest import auth, idem, make_event, make_users

RUSH = 50


async def test_fifty_users_rush_one_seat_exactly_one_wins(client, pool):
    """50 different users, one seat, all at once -> 1 x 201, 49 x 409.

    This is the headline test. It is only meaningful because:
      * the users are DIFFERENT (same-user duplicates would be caught by the
        one-hold-per-user rule instead of by the seat claim);
      * they share ONE seat id, so every request contends on the same Postgres
        row;
      * DB_POOL_MAX is 25 in the test env, so the requests really do arrive at
        the database concurrently rather than queueing on the pool.
    """
    event = await make_event(pool, rows=1, cols=1)
    seat = event["seat_ids"][0]
    tokens = await make_users(pool, RUSH)

    async def attempt(token: str):
        return await client.post(
            "/api/holds",
            json={"event_id": event["id"], "seat_ids": [seat]},
            headers={**auth(token), **idem(str(uuid.uuid4()))},
        )

    responses = await asyncio.gather(*(attempt(t) for t in tokens))
    codes = [r.status_code for r in responses]

    assert codes.count(201) == 1, f"expected exactly one winner, got {codes.count(201)}"
    assert codes.count(409) == RUSH - 1, f"status spread: {sorted(set(codes))}"
    assert all(r.json()["error"]["code"] == "seat_taken"
               for r in responses if r.status_code == 409)

    # The losers are told WHICH seat they lost, so the UI can highlight it.
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["error"]["conflicting_seat_ids"] == [seat]

    # Database truth: one hold, one held seat.
    assert await pool.fetchval(
        "SELECT count(*) FROM holds WHERE status = 'active'") == 1
    assert await pool.fetchval(
        "SELECT count(*) FROM seats WHERE status = 'held'") == 1


async def test_rush_on_a_small_venue_never_oversells(client, pool):
    """50 users, 10 seats, 2 seats each -> at most 5 winners and no seat twice.

    The multi-seat version matters because a partial claim is the dangerous
    case: a user who wins seat 3 but loses seat 4 must end up with NEITHER.
    """
    event = await make_event(pool, rows=2, cols=5)      # 10 seats
    seats = event["seat_ids"]
    tokens = await make_users(pool, RUSH)

    async def attempt(i: int, token: str):
        pair = [seats[(i * 2) % 10], seats[(i * 2 + 1) % 10]]
        return await client.post(
            "/api/holds",
            json={"event_id": event["id"], "seat_ids": pair},
            headers={**auth(token), **idem(str(uuid.uuid4()))},
        )

    responses = await asyncio.gather(
        *(attempt(i, t) for i, t in enumerate(tokens))
    )
    winners = [r for r in responses if r.status_code == 201]
    assert len(winners) <= 5
    assert {r.status_code for r in responses} <= {201, 409}

    # No seat is held by two holds, and no winner got a half-claim.
    held = await pool.fetch("SELECT id, hold_id FROM seats WHERE status = 'held'")
    assert len(held) == len(winners) * 2
    assert len({h["id"] for h in held}) == len(held)
    for w in winners:
        assert len(w.json()["seat_ids"]) == 2


async def test_all_or_nothing_partial_claim_rolls_back(client, pool):
    """One seat already taken -> the whole second request must leave no trace."""
    event = await make_event(pool, rows=1, cols=3)
    a, b, c = event["seat_ids"]
    t1, t2 = await make_users(pool, 2)

    first = await client.post("/api/holds",
                              json={"event_id": event["id"], "seat_ids": [b]},
                              headers={**auth(t1), **idem("k1")})
    assert first.status_code == 201

    # Asks for a (free), b (taken) and c (free).
    second = await client.post("/api/holds",
                               json={"event_id": event["id"], "seat_ids": [a, b, c]},
                               headers={**auth(t2), **idem("k2")})
    assert second.status_code == 409
    assert second.json()["error"]["conflicting_seat_ids"] == [b]

    # a and c must still be AVAILABLE: the transaction that briefly claimed
    # them was rolled back.
    rows = await pool.fetch(
        "SELECT id, status FROM seat_effective WHERE event_id = $1", event["id"]
    )
    statuses = {r["id"]: r["status"] for r in rows}
    assert statuses[a] == "available"
    assert statuses[c] == "available"
    assert statuses[b] == "held"
    # And no orphan hold row was left behind for the loser.
    assert await pool.fetchval("SELECT count(*) FROM holds") == 1


async def test_concurrent_confirms_of_one_hold_create_one_booking(client, pool):
    """The same user double-taps "Pay" 10 times with 10 DIFFERENT keys.

    Different keys mean the idempotency layer cannot help -- this is purely the
    FOR UPDATE lock on the hold row plus the status check doing the work. The
    first confirm wins; the rest see status='confirmed' and replay the same
    ticket instead of creating a second one.
    """
    event = await make_event(pool)
    tokens = await make_users(pool, 1)
    hold = await client.post("/api/holds",
                             json={"event_id": event["id"], "seat_ids": event["seat_ids"][:2]},
                             headers={**auth(tokens[0]), **idem("hold-key")})
    hold_id = hold.json()["hold_id"]

    responses = await asyncio.gather(*(
        client.post(f"/api/holds/{hold_id}/confirm",
                    headers={**auth(tokens[0]), **idem(str(uuid.uuid4()))})
        for _ in range(10)
    ))
    assert all(r.status_code == 200 for r in responses)
    codes = {r.json()["booking_code"] for r in responses}
    assert len(codes) == 1, "every caller must see the SAME booking"
    assert await pool.fetchval("SELECT count(*) FROM bookings") == 1
    assert sum(1 for r in responses if r.json()["already_confirmed"] is False) == 1


async def test_integrity_no_seat_in_two_bookings(client, pool, admin_token):
    """Run a full rush through to payment, then ask the database directly."""
    event = await make_event(pool, rows=2, cols=5)
    seats = event["seat_ids"]
    tokens = await make_users(pool, RUSH)

    async def rush(i: int, token: str):
        r = await client.post(
            "/api/holds",
            json={"event_id": event["id"], "seat_ids": [seats[i % 10]]},
            headers={**auth(token), **idem(str(uuid.uuid4()))},
        )
        if r.status_code == 201:
            await client.post(f"/api/holds/{r.json()['hold_id']}/confirm",
                              headers={**auth(token), **idem(str(uuid.uuid4()))})
        return r.status_code

    await asyncio.gather(*(rush(i, t) for i, t in enumerate(tokens)))

    stats = (await client.get(f"/api/admin/events/{event['id']}/stats",
                              headers=auth(admin_token))).json()
    assert stats["seats_in_two_bookings"] == 0
    assert stats["booked"] <= stats["total_seats"]
    assert stats["booked_seat_rows"] <= stats["total_seats"]
    assert stats["integrity_ok"] is True

    # The same check straight from SQL, independent of the endpoint.
    dupes = await pool.fetchval(
        """
        SELECT count(*) FROM (
            SELECT seat_id FROM booking_seats GROUP BY seat_id HAVING count(*) > 1
        ) d
        """
    )
    assert dupes == 0
