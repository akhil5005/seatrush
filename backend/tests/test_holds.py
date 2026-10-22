"""Hold rules, the seat map, and the booking flow."""

import uuid

from app import config

from .conftest import auth, idem, make_event


def key() -> dict:
    return idem(str(uuid.uuid4()))


async def test_hold_before_sale_opens_is_rejected(client, user_token, pool):
    event = await make_event(pool, sale_opens_in_seconds=600)   # opens in 10 min
    r = await client.post("/api/holds",
                          json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                          headers={**auth(user_token), **key()})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "sale_not_open"
    # And nothing was written -- the whole transaction rolled back.
    assert await pool.fetchval("SELECT count(*) FROM holds") == 0
    assert await pool.fetchval(
        "SELECT count(*) FROM seats WHERE status <> 'available'") == 0


async def test_hold_respects_max_seats_per_hold(client, user_token, pool):
    event = await make_event(pool, rows=2, cols=5)   # 10 seats
    too_many = event["seat_ids"][: config.MAX_SEATS_PER_HOLD + 1]
    r = await client.post("/api/holds",
                          json={"event_id": event["id"], "seat_ids": too_many},
                          headers={**auth(user_token), **key()})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "seat_limit"


async def test_one_active_hold_per_user_per_event(client, user_token, pool):
    event = await make_event(pool)
    first = await client.post("/api/holds",
                              json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                              headers={**auth(user_token), **key()})
    assert first.status_code == 201

    second = await client.post("/api/holds",
                               json={"event_id": event["id"], "seat_ids": [event["seat_ids"][1]]},
                               headers={**auth(user_token), **key()})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "hold_limit"
    # The anti-hoarding error points at the hold the user already has, so the
    # UI can send them to checkout instead of just saying "no".
    assert second.json()["error"]["hold_id"] == first.json()["hold_id"]


async def test_duplicate_seat_ids_are_deduplicated(client, user_token, pool):
    """Asking for seat 5 twice must count as one seat, not two -- otherwise the
    claim's RETURNING count would never match the request and every such
    request would 409."""
    event = await make_event(pool)
    seat = event["seat_ids"][0]
    r = await client.post("/api/holds",
                          json={"event_id": event["id"], "seat_ids": [seat, seat, seat]},
                          headers={**auth(user_token), **key()})
    assert r.status_code == 201
    assert r.json()["seat_ids"] == [seat]
    assert r.json()["total_cents"] == event["price_cents"]


async def test_unknown_seat_id_is_404_not_409(client, user_token, pool):
    event = await make_event(pool)
    r = await client.post("/api/holds",
                          json={"event_id": event["id"], "seat_ids": [999999]},
                          headers={**auth(user_token), **key()})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "seat_not_found"


async def test_seat_map_never_reveals_the_holder(client, user_token, pool):
    """The seat map must stay user-independent, because Nginx microcaches it
    and serves one caller's copy to the next."""
    event = await make_event(pool)
    await client.post("/api/holds",
                      json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                      headers={**auth(user_token), **key()})
    body = (await client.get(f"/api/events/{event['id']}/seats")).json()
    assert body["seats"][0]["status"] == "held"
    assert set(body["seats"][0]) == {"id", "label", "row_idx", "col_idx", "status"}
    serialised = str(body)
    assert "user_id" not in serialised and "hold_id" not in serialised


async def test_seats_left_counts_only_available(client, user_token, pool):
    event = await make_event(pool, rows=2, cols=5)      # 10 seats
    await client.post("/api/holds",
                      json={"event_id": event["id"], "seat_ids": event["seat_ids"][:3]},
                      headers={**auth(user_token), **key()})
    assert (await client.get(f"/api/events/{event['id']}/seats")).json()["seats_left"] == 7
    listed = (await client.get("/api/events")).json()["events"][0]
    assert listed["seats_left"] == 7
    assert listed["sale_open"] is True


async def test_full_booking_flow(client, user_token, pool):
    event = await make_event(pool, price_cents=25000)
    seats = event["seat_ids"][:2]

    hold = await client.post("/api/holds",
                             json={"event_id": event["id"], "seat_ids": seats},
                             headers={**auth(user_token), **key()})
    assert hold.status_code == 201
    hold_id = hold.json()["hold_id"]
    assert hold.json()["total_cents"] == 50000

    detail = await client.get(f"/api/holds/{hold_id}", headers=auth(user_token))
    assert detail.status_code == 200
    assert detail.json()["expired"] is False
    assert detail.json()["labels"] == ["A1", "A2"]

    paid = await client.post(f"/api/holds/{hold_id}/confirm",
                             headers={**auth(user_token), **key()})
    assert paid.status_code == 200
    assert paid.json()["booking_code"].startswith("SR-")
    assert paid.json()["already_confirmed"] is False

    tickets = (await client.get("/api/bookings", headers=auth(user_token))).json()
    assert len(tickets["bookings"]) == 1
    assert tickets["bookings"][0]["labels"] == ["A1", "A2"]
    assert tickets["bookings"][0]["total_cents"] == 50000

    # Seats are now terminal: booked, and gone from the available count.
    smap = (await client.get(f"/api/events/{event['id']}/seats")).json()
    assert [s["status"] for s in smap["seats"][:2]] == ["booked", "booked"]
    assert smap["seats_left"] == 8


async def test_hold_detail_is_owner_only(client, user_token, admin_token, pool):
    event = await make_event(pool)
    hold = await client.post("/api/holds",
                             json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                             headers={**auth(user_token), **key()})
    hold_id = hold.json()["hold_id"]
    # Even an admin is not the owner. 404, not 403, so a guessed id is not
    # confirmed to exist.
    r = await client.get(f"/api/holds/{hold_id}", headers=auth(admin_token))
    assert r.status_code == 404


async def test_release_frees_the_seats(client, user_token, pool):
    event = await make_event(pool)
    seats = event["seat_ids"][:2]
    hold = await client.post("/api/holds",
                             json={"event_id": event["id"], "seat_ids": seats},
                             headers={**auth(user_token), **key()})
    hold_id = hold.json()["hold_id"]

    r = await client.delete(f"/api/holds/{hold_id}", headers=auth(user_token))
    assert r.status_code == 200
    assert r.json()["seats_freed"] == 2

    smap = (await client.get(f"/api/events/{event['id']}/seats")).json()
    assert smap["seats_left"] == len(event["seat_ids"])

    # DELETE is naturally idempotent: a retry is a 200 no-op, not an error.
    again = await client.delete(f"/api/holds/{hold_id}", headers=auth(user_token))
    assert again.status_code == 200
    assert again.json()["seats_freed"] == 0

    # Releasing frees the per-user hold slot, so the user can pick again.
    retry = await client.post("/api/holds",
                              json={"event_id": event["id"], "seat_ids": seats},
                              headers={**auth(user_token), **key()})
    assert retry.status_code == 201


async def test_confirmed_hold_cannot_be_released(client, user_token, pool):
    event = await make_event(pool)
    hold = await client.post("/api/holds",
                             json={"event_id": event["id"], "seat_ids": [event["seat_ids"][0]]},
                             headers={**auth(user_token), **key()})
    hold_id = hold.json()["hold_id"]
    await client.post(f"/api/holds/{hold_id}/confirm",
                      headers={**auth(user_token), **key()})
    r = await client.delete(f"/api/holds/{hold_id}", headers=auth(user_token))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "hold_confirmed"
