"""My tickets.

Private per user, therefore never cached by Nginx -- a shared cache keyed only
on the URL would hand one user's tickets to the next caller of the same URL.
That is the single rule that decides what may be microcached in this app:
"does the response depend on who asked?"
"""

from fastapi import APIRouter, Depends

from . import db
from .auth import User, current_user

router = APIRouter(prefix="/api/bookings", tags=["bookings"])


@router.get("")
async def my_bookings(user: User = Depends(current_user)):
    """One query, no N+1: the seat labels are aggregated in SQL.

    booking_seats is the authoritative list of seats in a booking (seats.hold_id
    is only an audit trail), so the join goes through it.
    """
    rows = await db.pool().fetch(
        """
        SELECT b.id, b.booking_code, b.total_cents, b.created_at,
               e.id AS event_id, e.title, e.venue, e.starts_at,
               COALESCE(
                   (SELECT array_agg(s.label ORDER BY s.row_idx, s.col_idx)
                      FROM booking_seats bs
                      JOIN seats s ON s.id = bs.seat_id
                     WHERE bs.booking_id = b.id),
                   '{}'
               ) AS labels
          FROM bookings b
          JOIN events e ON e.id = b.event_id
         WHERE b.user_id = $1
         ORDER BY b.created_at DESC
        """,
        user.id,
    )
    return {
        "bookings": [
            {
                "booking_id": str(r["id"]),
                "booking_code": r["booking_code"],
                "event_id": r["event_id"],
                "event_title": r["title"],
                "venue": r["venue"],
                "starts_at": r["starts_at"],
                "labels": list(r["labels"] or []),
                "seat_count": len(r["labels"] or []),
                "total_cents": r["total_cents"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }
