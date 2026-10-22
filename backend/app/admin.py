"""Admin endpoints: create an event, reset it for a demo, and prove integrity.

All of these require role='admin' (checked in auth.current_admin from the JWT's
`role` claim, so no database round trip). Nginx never caches /api/admin/*.
"""

import datetime as dt
import string

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from . import db
from .auth import User, current_admin
from .errors import ApiError

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Venue size cap. 10 rows x 20 cols = 200 seats, which keeps the seat map
# readable on a laptop screen and the seat-map JSON under a few KB.
MAX_ROWS = 10
MAX_COLS = 20


class EventRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    venue: str = Field(min_length=1, max_length=200)
    starts_at: dt.datetime
    sale_opens_at: dt.datetime
    rows: int = Field(ge=1, le=MAX_ROWS)
    cols: int = Field(ge=1, le=MAX_COLS)
    price_cents: int = Field(ge=0, le=100_000_00)

    @field_validator("starts_at", "sale_opens_at")
    @classmethod
    def _require_timezone(cls, v: dt.datetime) -> dt.datetime:
        # A naive datetime would be stored as if it were UTC and the countdown
        # would be wrong by the user's offset. Force the client to be explicit.
        if v.tzinfo is None:
            raise ValueError("must include a timezone offset (e.g. 2026-01-01T18:00:00Z)")
        return v


@router.post("/events", status_code=201)
async def create_event(body: EventRequest, _admin: User = Depends(current_admin)):
    """Create an event and generate its seats in the same transaction.

    Atomicity matters: an event with a half-generated seat map would show a
    partial venue to every user who loaded it. Either the whole venue exists or
    the event does not.
    """
    async with db.pool().acquire() as con:
        async with con.transaction():
            event = await con.fetchrow(
                """
                INSERT INTO events (title, venue, starts_at, sale_opens_at,
                                    rows, cols, price_cents)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, sale_opens_at
                """,
                body.title, body.venue, body.starts_at, body.sale_opens_at,
                body.rows, body.cols, body.price_cents,
            )
            seats = _seat_rows(event["id"], body.rows, body.cols)
            # executemany sends one prepared statement with many parameter sets
            # instead of 200 separate round trips.
            await con.executemany(
                """
                INSERT INTO seats (event_id, label, row_idx, col_idx, status)
                VALUES ($1, $2, $3, $4, 'available')
                """,
                seats,
            )
    return {
        "id": event["id"],
        "seats_created": len(seats),
        "sale_opens_at": event["sale_opens_at"].isoformat(),
    }


def _seat_rows(event_id: int, rows: int, cols: int) -> list[tuple]:
    """Rows are letters (A, B, C...), columns are 1-based numbers: 'C7'."""
    out = []
    for r in range(rows):
        letter = string.ascii_uppercase[r]
        for c in range(cols):
            out.append((event_id, f"{letter}{c + 1}", r, c))
    return out


@router.post("/events/{event_id}/reset")
async def reset_event(event_id: int, _admin: User = Depends(current_admin)):
    """Demo helper: wipe all holds/bookings for an event, free every seat.

    Needed because a live demo runs the flash-sale load test more than once and
    each run needs a clean venue. Deliberately admin-only and scoped to one
    event -- it is destructive.

    Order matters: booking_seats and bookings reference seats/holds, so seats
    are detached first, then the dependent rows go (ON DELETE CASCADE from
    bookings handles booking_seats).
    """
    async with db.pool().acquire() as con:
        async with con.transaction():
            exists = await con.fetchval("SELECT 1 FROM events WHERE id = $1", event_id)
            if not exists:
                raise ApiError(404, "event_not_found", "No such event")

            # 1. Detach every seat from its hold, so nothing points at the rows
            #    we are about to delete.
            freed = await con.fetch(
                """
                UPDATE seats SET status = 'available', hold_id = NULL
                 WHERE event_id = $1
                RETURNING id
                """,
                event_id,
            )
            # 2. Bookings go next; booking_seats follows via ON DELETE CASCADE.
            bookings = await con.fetch(
                "DELETE FROM bookings WHERE event_id = $1 RETURNING id", event_id
            )
            # 3. Finally the holds themselves.
            holds = await con.fetch(
                "DELETE FROM holds WHERE event_id = $1 RETURNING id", event_id
            )
    return {
        "reset": True,
        "event_id": event_id,
        "seats_freed": len(freed),
        "bookings_deleted": len(bookings),
        "holds_deleted": len(holds),
    }


@router.get("/loadtest-users")
async def loadtest_users(limit: int = 300, _admin: User = Depends(current_admin)):
    """Ids of the seeded ltNNN@seatrush.dev fixtures. DEMO TOOLING ONLY.

    Why this exists: loadtest/flashsale.py needs to act as hundreds of distinct
    users. Logging each one in would hit Nginx's login rate limit (5r/m per IP)
    within seconds and measure the rate limiter instead of the booking logic.
    So the load test mints its own JWTs with the dev secret -- and to do that it
    needs the real `sub` (user id) for each fixture account, because holds.user_id
    is a foreign key to a row that must actually exist.

    Scoped to the lt% fixtures on purpose: it can never return a real user, and
    it returns no password hashes. In production this endpoint would not ship --
    it exists so the load test does not need database credentials of its own.
    """
    rows = await db.pool().fetch(
        """
        SELECT id, email FROM users
         WHERE email LIKE 'lt%@seatrush.dev' AND role = 'user'
         ORDER BY email
         LIMIT $1
        """,
        max(1, min(limit, 2000)),
    )
    return {"users": [{"id": r["id"], "email": r["email"]} for r in rows]}


@router.get("/events/{event_id}/stats")
async def event_stats(event_id: int, _admin: User = Depends(current_admin)):
    """Live counters plus the integrity check the load test asserts on.

    `seats_in_two_bookings` must be 0 forever. It is computed from
    booking_seats, independently of any application logic -- if the conditional
    UPDATE in holds.py were ever broken, this number is how we would find out.
    UNIQUE (seat_id) means the database cannot even store a non-zero answer, so
    this query is the proof that the constraint is doing its job.
    """
    row = await db.pool().fetchrow(
        """
        SELECT
          (SELECT count(*) FROM events WHERE id = $1)                       AS event_exists,
          (SELECT count(*) FROM seats WHERE event_id = $1)                  AS total_seats,
          -- These three read the lazy-expiry view, so a seat whose hold aged
          -- out counts as available, exactly as the public seat map reports it.
          (SELECT count(*) FROM seat_effective
            WHERE event_id = $1 AND status = 'available')                   AS available,
          (SELECT count(*) FROM seat_effective
            WHERE event_id = $1 AND status = 'held')                        AS held,
          (SELECT count(*) FROM seat_effective
            WHERE event_id = $1 AND status = 'booked')                      AS booked,
          (SELECT count(*) FROM bookings WHERE event_id = $1)               AS bookings,
          (SELECT count(*) FROM holds
            WHERE event_id = $1 AND status = 'active' AND expires_at < now()) AS expired_holds,
          (SELECT count(*) FROM (
                SELECT bs.seat_id
                  FROM booking_seats bs
                  JOIN bookings b ON b.id = bs.booking_id
                 WHERE b.event_id = $1
                 GROUP BY bs.seat_id
                HAVING count(*) > 1
           ) dupes)                                                         AS seats_in_two_bookings,
          (SELECT count(*) FROM booking_seats bs
             JOIN bookings b ON b.id = bs.booking_id
            WHERE b.event_id = $1)                                          AS booked_seat_rows
        """,
        event_id,
    )
    if not row["event_exists"]:
        raise ApiError(404, "event_not_found", "No such event")

    return {
        "event_id": event_id,
        "total_seats": row["total_seats"],
        "available": row["available"],
        "held": row["held"],
        "booked": row["booked"],
        "bookings": row["bookings"],
        "expired_holds": row["expired_holds"],
        "booked_seat_rows": row["booked_seat_rows"],
        "seats_in_two_bookings": row["seats_in_two_bookings"],
        # Single boolean the load test and the admin UI both key off.
        "integrity_ok": row["seats_in_two_bookings"] == 0
                        and row["booked_seat_rows"] <= row["total_seats"],
    }
