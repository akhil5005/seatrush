"""Holds and bookings -- the concurrency core of SeatRush.

Read this file top to bottom; it is the part the whole project exists to prove.

The invariant: at no point, under any amount of concurrency, may two users end
up owning the same seat. Three independent mechanisms enforce it:

  1. A single conditional UPDATE claims seats (claim_seats below). Postgres row
     locks serialise concurrent claims on the same row.
  2. One explicit transaction per write, so a partial claim never commits.
  3. UNIQUE (seat_id) on booking_seats -- defence in depth, in the database.
"""

import secrets
import uuid

import asyncpg
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config, db, idempotency
from .auth import User, current_user
from .errors import ApiError

router = APIRouter(prefix="/api/holds", tags=["holds"])

# Unambiguous alphabet for a code read aloud or typed from a screenshot:
# no 0/O, no 1/I/L.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def _booking_code() -> str:
    return "SR-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def _iso(value):
    """Datetimes -> ISO strings before the dict ever becomes a response.

    This matters for idempotency: the response body is stored as jsonb and
    replayed verbatim on a retry, so it must already be plain JSON types.
    Otherwise the replay would differ from the original by date formatting
    alone, and "identical response" would be a lie.
    """
    return value.isoformat() if value is not None else None


class HoldRequest(BaseModel):
    event_id: int
    # The per-request cap is also enforced against config below, but declaring a
    # max here means an absurd payload is rejected before any DB work.
    seat_ids: list[int] = Field(min_length=1, max_length=200)


# --------------------------------------------------------------------------- #
# The claim
# --------------------------------------------------------------------------- #

CLAIM_SEATS_SQL = """
UPDATE seats
   SET status = 'held', hold_id = $1
 WHERE event_id = $2
   AND id = ANY($3::bigint[])
   AND (status = 'available'
        OR (status = 'held' AND hold_id IN (
              SELECT id FROM holds
               WHERE status = 'active' AND expires_at < now())))
RETURNING id
"""


async def claim_seats(con: asyncpg.Connection, hold_id: uuid.UUID,
                      event_id: int, seat_ids: list[int]) -> set[int]:
    """Atomically take every requested seat, or effectively take none.

    WHY THIS IS RACE-FREE
      Two users send overlapping seat sets at the same millisecond. Both
      statements reach the same seat row.

      - T1 gets there first. The UPDATE takes a ROW-level exclusive lock on
        that seat tuple and writes status='held'. It has not committed yet.
      - T2's UPDATE scans to the same row, finds it write-locked, and BLOCKS.
        It is not spinning and it is not reading stale data -- it is queued on
        that one tuple.
      - T1 commits. T2 wakes up and, because we are at READ COMMITTED,
        Postgres re-reads the *new* version of the row and RE-EVALUATES the
        WHERE clause against it (the EvalPlanQual recheck). The row now says
        status='held' with a live hold, so it no longer matches. T2 matches
        ZERO rows for that seat.
      - T2's RETURNING count is therefore smaller than the number of seats it
        asked for, so we raise 409 seat_taken and the whole transaction rolls
        back -- including any *other* seats T2 had managed to grab, which is
        what makes a multi-seat hold all-or-nothing.

      The re-evaluation is the crux. A naive "SELECT, inspect, then UPDATE"
      would read "available", block on nothing, and both users would write --
      the classic lost update. Here the check and the write are the same
      statement, so there is no window between them.

    WHY NOT SELECT ... FOR UPDATE THEN UPDATE?
      It also works, and it is the textbook answer:
          SELECT id FROM seats WHERE id = ANY($1) AND ... FOR UPDATE;
          -- inspect in Python, then UPDATE
      We chose the conditional UPDATE because it is ONE round trip instead of
      two, it cannot be got wrong by someone later inserting code between the
      SELECT and the UPDATE, and the eligibility rule lives in exactly one
      place. FOR UPDATE's advantage -- inspecting each row before deciding --
      buys nothing here, because our decision is all-or-nothing anyway.
      (FOR UPDATE SKIP LOCKED would be actively wrong: it would silently hand
      the user a different seat than the one they clicked.)

    Returns the set of seat ids actually claimed.
    """
    try:
        rows = await con.fetch(CLAIM_SEATS_SQL, hold_id, event_id, seat_ids)
    except asyncpg.DeadlockDetectedError:
        # Possible when two transactions lock the same seats in opposite order.
        # Postgres breaks the cycle by aborting one of them; for the client that
        # is indistinguishable from losing the race, so report it as such.
        raise ApiError(409, "seat_taken",
                       "Those seats are being taken right now, please pick again")
    return {r["id"] for r in rows}


# --------------------------------------------------------------------------- #
# POST /api/holds
# --------------------------------------------------------------------------- #

@router.post("", status_code=201)
async def create_hold(
    body: HoldRequest,
    user: User = Depends(current_user),
    key: str = Depends(idempotency.require_key),
):
    # De-duplicate first: asking for seat 5 twice must not count as two seats
    # and must not make the claim count mismatch.
    seat_ids = sorted(set(body.seat_ids))
    if len(seat_ids) > config.MAX_SEATS_PER_HOLD:
        raise ApiError(400, "seat_limit",
                       f"You can hold at most {config.MAX_SEATS_PER_HOLD} seats at a time")

    payload = {"event_id": body.event_id, "seat_ids": seat_ids}

    async with db.pool().acquire() as con:
        # ONE transaction for: the idempotency record, the hold row and the
        # seat claim. Any raise below unwinds this block, asyncpg ROLLBACKs,
        # and the database is exactly as it was before the request.
        async with con.transaction():
            claimed, status, stored = await idempotency.claim(
                con, user.id, key, "POST /api/holds", payload
            )
            if not claimed:
                return JSONResponse(status_code=status, content=stored)

            # --- the flash-sale gate, decided by the DATABASE clock ---------
            event = await con.fetchrow(
                """
                SELECT id, price_cents, sale_opens_at,
                       (now() >= sale_opens_at) AS sale_open
                  FROM events WHERE id = $1
                """,
                body.event_id,
            )
            if event is None:
                raise ApiError(404, "event_not_found", "No such event")
            if not event["sale_open"]:
                raise ApiError(403, "sale_not_open",
                               "Booking has not opened for this event",
                               sale_opens_at=_iso(event["sale_opens_at"]))

            # --- do the requested seats even belong to this event? ----------
            seat_rows = await con.fetch(
                "SELECT id, label FROM seats WHERE event_id = $1 AND id = ANY($2::bigint[])",
                body.event_id, seat_ids,
            )
            if len(seat_rows) != len(seat_ids):
                raise ApiError(404, "seat_not_found",
                               "One or more seats do not exist for this event")
            labels = {r["id"]: r["label"] for r in seat_rows}

            # --- anti-hoarding: one live hold per user per event ------------
            # Without this, a bot could open 30 tabs and park the whole venue
            # for HOLD_SECONDS without ever paying.
            existing = await con.fetchrow(
                """
                SELECT id FROM holds
                 WHERE user_id = $1 AND event_id = $2
                   AND status = 'active' AND expires_at > now()
                 LIMIT 1
                """,
                user.id, body.event_id,
            )
            if existing is not None:
                raise ApiError(409, "hold_limit",
                               "You already have seats on hold for this event",
                               hold_id=str(existing["id"]))

            # --- the hold row must exist before seats can point at it -------
            hold_id = uuid.uuid4()
            hold = await con.fetchrow(
                """
                INSERT INTO holds (id, user_id, event_id, expires_at, status)
                VALUES ($1, $2, $3, now() + make_interval(secs => $4), 'active')
                RETURNING id, expires_at
                """,
                hold_id, user.id, body.event_id, float(config.HOLD_SECONDS),
            )

            got = await claim_seats(con, hold_id, body.event_id, seat_ids)
            if len(got) != len(seat_ids):
                lost = [s for s in seat_ids if s not in got]
                # Raising here rolls back the hold row AND the seats we did
                # win, so a partially successful claim never becomes visible.
                raise ApiError(409, "seat_taken",
                               "Some of those seats were just taken",
                               conflicting_seat_ids=lost,
                               conflicting_labels=[labels[s] for s in lost])

            response = {
                "hold_id": str(hold_id),
                "event_id": body.event_id,
                "seat_ids": seat_ids,
                "labels": [labels[s] for s in seat_ids],
                "expires_at": _iso(hold["expires_at"]),
                "price_cents": event["price_cents"],
                "total_cents": event["price_cents"] * len(seat_ids),
                "status": "active",
            }
            await idempotency.store(con, user.id, key, 201, response)
            return JSONResponse(status_code=201, content=response)


# --------------------------------------------------------------------------- #
# GET /api/holds/{id}
# --------------------------------------------------------------------------- #

_HOLD_DETAIL_SQL = """
SELECT h.id, h.user_id, h.event_id, h.expires_at, h.status,
       (h.expires_at < now()) AS expired,
       e.title, e.venue, e.starts_at, e.price_cents,
       b.booking_code,
       COALESCE((SELECT array_agg(s.label ORDER BY s.row_idx, s.col_idx)
                   FROM seats s WHERE s.hold_id = h.id), '{}') AS labels,
       COALESCE((SELECT array_agg(s.id ORDER BY s.row_idx, s.col_idx)
                   FROM seats s WHERE s.hold_id = h.id), '{}') AS seat_ids
  FROM holds h
  JOIN events e ON e.id = h.event_id
  LEFT JOIN bookings b ON b.hold_id = h.id
 WHERE h.id = $1
"""


@router.get("/{hold_id}")
async def get_hold(hold_id: uuid.UUID, user: User = Depends(current_user)):
    """Owner-only. Never Nginx-cached: the answer depends on who is asking."""
    row = await db.pool().fetchrow(_HOLD_DETAIL_SQL, hold_id)
    if row is None or row["user_id"] != user.id:
        # 404 rather than 403 for a hold owned by someone else: a wrong guess
        # should not confirm that the id exists.
        raise ApiError(404, "hold_not_found", "No such hold")

    seat_ids = list(row["seat_ids"] or [])
    return {
        "hold_id": str(row["id"]),
        "event_id": row["event_id"],
        "event_title": row["title"],
        "venue": row["venue"],
        "starts_at": _iso(row["starts_at"]),
        "seat_ids": seat_ids,
        "labels": list(row["labels"] or []),
        "expires_at": _iso(row["expires_at"]),
        "expired": row["expired"],
        "status": row["status"],
        "price_cents": row["price_cents"],
        "total_cents": row["price_cents"] * len(seat_ids),
        "booking_code": row["booking_code"],
    }


# --------------------------------------------------------------------------- #
# POST /api/holds/{id}/confirm   -- the "payment"
# --------------------------------------------------------------------------- #

@router.post("/{hold_id}/confirm")
async def confirm_hold(
    hold_id: uuid.UUID,
    user: User = Depends(current_user),
    key: str = Depends(idempotency.require_key),
):
    """Turn a hold into a booking.

    There is no real payment -- the "Pay" button stands in for a payment
    provider's callback, which is exactly the kind of call that gets retried
    and therefore the reason this endpoint needs an idempotency key.
    """
    payload = {"hold_id": str(hold_id)}

    async with db.pool().acquire() as con:
        async with con.transaction():
            claimed, status, stored = await idempotency.claim(
                con, user.id, key, "POST /api/holds/{id}/confirm", payload
            )
            if not claimed:
                return JSONResponse(status_code=status, content=stored)

            # FOR UPDATE here, not a conditional UPDATE, because we must read
            # several fields and then do multi-table work while still holding
            # the row. Locking the hold row serialises two confirms of the same
            # hold: the second waits, then sees status='confirmed'.
            hold = await con.fetchrow(
                """
                SELECT h.id, h.user_id, h.event_id, h.status, h.expires_at,
                       (h.expires_at < now()) AS expired,
                       e.price_cents
                  FROM holds h JOIN events e ON e.id = h.event_id
                 WHERE h.id = $1
                   FOR UPDATE OF h
                """,
                hold_id,
            )
            if hold is None or hold["user_id"] != user.id:
                raise ApiError(404, "hold_not_found", "No such hold")

            if hold["status"] == "confirmed":
                # Already paid. A double-click that generated a *new* key lands
                # here; returning the existing ticket is friendlier than an
                # error and is still exactly-once, because we create nothing.
                existing = await con.fetchrow(
                    """
                    SELECT b.id, b.booking_code, b.total_cents, b.event_id,
                           COALESCE((SELECT array_agg(s.label ORDER BY s.row_idx, s.col_idx)
                                       FROM seats s WHERE s.hold_id = b.hold_id), '{}') AS labels,
                           COALESCE((SELECT array_agg(s.id ORDER BY s.row_idx, s.col_idx)
                                       FROM seats s WHERE s.hold_id = b.hold_id), '{}') AS seat_ids
                      FROM bookings b WHERE b.hold_id = $1
                    """,
                    hold_id,
                )
                response = {
                    "booking_id": str(existing["id"]),
                    "booking_code": existing["booking_code"],
                    "event_id": existing["event_id"],
                    "seat_ids": list(existing["seat_ids"] or []),
                    "labels": list(existing["labels"] or []),
                    "total_cents": existing["total_cents"],
                    "already_confirmed": True,
                }
                await idempotency.store(con, user.id, key, 200, response)
                return JSONResponse(status_code=200, content=response)

            if hold["status"] == "released":
                raise ApiError(410, "hold_released", "That hold was released")
            if hold["expired"]:
                # 410 Gone, not 409 Conflict: the hold is not contested, it has
                # ceased to exist as a claim. Its seats are already free.
                raise ApiError(410, "hold_expired",
                               "Your hold expired, please pick seats again",
                               expires_at=_iso(hold["expires_at"]))

            seat_rows = await con.fetch(
                """
                SELECT id, label FROM seats
                 WHERE hold_id = $1 AND status = 'held'
                 ORDER BY row_idx, col_idx
                """,
                hold_id,
            )
            if not seat_rows:
                # Belt and braces: an active, unexpired hold always owns seats.
                raise ApiError(410, "hold_expired", "Your hold no longer owns any seats")

            seat_ids = [r["id"] for r in seat_rows]
            total_cents = hold["price_cents"] * len(seat_ids)
            booking_id = uuid.uuid4()

            code = await _insert_booking(con, booking_id, user.id, hold["event_id"],
                                         hold_id, total_cents)
            try:
                await con.executemany(
                    "INSERT INTO booking_seats (booking_id, seat_id) VALUES ($1, $2)",
                    [(booking_id, sid) for sid in seat_ids],
                )
            except asyncpg.UniqueViolationError:
                # UNIQUE (seat_id) fired: this seat is already in someone
                # else's booking. Reaching here would mean the claim logic has
                # a bug -- and the database still refuses to oversell. The
                # transaction rolls back and the user gets an ordinary 409.
                raise ApiError(409, "seat_taken",
                               "One of those seats has already been booked")

            # Terminal state for the seats. hold_id is left in place as an audit
            # trail of which hold produced the booking.
            await con.execute(
                "UPDATE seats SET status = 'booked' WHERE hold_id = $1", hold_id
            )
            await con.execute(
                "UPDATE holds SET status = 'confirmed' WHERE id = $1", hold_id
            )

            response = {
                "booking_id": str(booking_id),
                "booking_code": code,
                "event_id": hold["event_id"],
                "seat_ids": seat_ids,
                "labels": [r["label"] for r in seat_rows],
                "total_cents": total_cents,
                "already_confirmed": False,
            }
            await idempotency.store(con, user.id, key, 200, response)
            return JSONResponse(status_code=200, content=response)


async def _insert_booking(con, booking_id, user_id, event_id, hold_id, total_cents) -> str:
    """Insert the booking, retrying only on a booking_code collision.

    The nested con.transaction() is a SAVEPOINT: a unique violation aborts only
    back to the savepoint, so the outer transaction survives and we can draw
    another code. Without the savepoint, any failed INSERT would poison the
    whole transaction ("current transaction is aborted, commands ignored").
    """
    for _ in range(5):
        code = _booking_code()
        try:
            async with con.transaction():
                await con.execute(
                    """
                    INSERT INTO bookings (id, user_id, event_id, hold_id,
                                          booking_code, total_cents)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    booking_id, user_id, event_id, hold_id, code, total_cents,
                )
            return code
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "bookings_booking_code_key":
                continue  # one-in-a-billion collision, draw again
            # bookings_hold_id_key -> this hold already produced a booking.
            raise ApiError(409, "hold_already_booked", "That hold is already booked")
    raise ApiError(500, "booking_code_exhausted", "Could not allocate a booking code")


# --------------------------------------------------------------------------- #
# DELETE /api/holds/{id}  -- give the seats back
# --------------------------------------------------------------------------- #

@router.delete("/{hold_id}")
async def release_hold(hold_id: uuid.UUID, user: User = Depends(current_user)):
    """No Idempotency-Key needed: DELETE is naturally idempotent.

    Releasing an already-released hold is a no-op that still returns 200, so a
    retry is harmless by construction rather than by bookkeeping.
    """
    async with db.pool().acquire() as con:
        async with con.transaction():
            hold = await con.fetchrow(
                "SELECT id, user_id, status FROM holds WHERE id = $1 FOR UPDATE",
                hold_id,
            )
            if hold is None or hold["user_id"] != user.id:
                raise ApiError(404, "hold_not_found", "No such hold")
            if hold["status"] == "confirmed":
                raise ApiError(409, "hold_confirmed",
                               "That hold is already booked and cannot be released")

            # Only 'held' seats go back: a seat somehow already booked must
            # never be freed by a release.
            freed = await con.fetch(
                """
                UPDATE seats SET status = 'available', hold_id = NULL
                 WHERE hold_id = $1 AND status = 'held'
                 RETURNING id
                """,
                hold_id,
            )
            await con.execute(
                "UPDATE holds SET status = 'released' WHERE id = $1", hold_id
            )
            return {"released": True, "hold_id": str(hold_id),
                    "seats_freed": len(freed)}
