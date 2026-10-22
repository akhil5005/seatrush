"""Public, cacheable reads.

The single rule that makes these endpoints safe to microcache in Nginx: the
response must NOT depend on who is asking. No Authorization header is read, no
cookie is set, and the seat map never reveals *which* user holds a seat -- only
that it is unavailable. If any of those were untrue, Nginx would be serving one
user's view to another, which is the classic shared-cache data leak.

Everything reads the `seat_effective` view (schema.sql), so a seat whose hold
has aged out is reported as available without any sweeper having run.
"""

from fastapi import APIRouter

from . import db
from .errors import ApiError

router = APIRouter(prefix="/api/events", tags=["events"])

# Shared projection so list and detail can never disagree about a field name.
_EVENT_COLS = """
    e.id, e.title, e.venue, e.starts_at, e.sale_opens_at,
    e.rows, e.cols, e.price_cents,
    (e.rows * e.cols)                       AS total_seats,
    (now() >= e.sale_opens_at)              AS sale_open,
    (SELECT count(*) FROM seat_effective se
      WHERE se.event_id = e.id AND se.status = 'available') AS seats_left
"""


def _event_json(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "venue": row["venue"],
        "starts_at": row["starts_at"],
        "sale_opens_at": row["sale_opens_at"],
        "rows": row["rows"],
        "cols": row["cols"],
        "price_cents": row["price_cents"],
        "total_seats": row["total_seats"],
        "sale_open": row["sale_open"],
        "seats_left": row["seats_left"],
        "sold_out": row["seats_left"] == 0,
    }


@router.get("")
async def list_events():
    """Event list with live seats_left. Microcached by Nginx for 2 s."""
    rows = await db.pool().fetch(
        f"SELECT {_EVENT_COLS} FROM events e ORDER BY e.sale_opens_at, e.id"
    )
    return {"events": [_event_json(r) for r in rows]}


@router.get("/{event_id}")
async def get_event(event_id: int):
    row = await db.pool().fetchrow(
        f"SELECT {_EVENT_COLS} FROM events e WHERE e.id = $1", event_id
    )
    if row is None:
        raise ApiError(404, "event_not_found", "No such event")
    return _event_json(row)


@router.get("/{event_id}/seats")
async def get_seats(event_id: int):
    """Seat-map snapshot. Microcached by Nginx for 1 s.

    This is the endpoint every open browser tab polls every ~2.5 s, so it is
    the one that would melt the backend during a flash sale -- and therefore
    the one the microcache exists for. A cached map can be up to 1 s stale; a
    user may click a seat someone just took and get a clean 409 seat_taken,
    because seat ownership is decided ONLY by Postgres, never by this snapshot.
    """
    rows = await db.pool().fetch(
        """
        SELECT id, label, row_idx, col_idx, status
        FROM seat_effective
        WHERE event_id = $1
        ORDER BY row_idx, col_idx
        """,
        event_id,
    )
    if not rows:
        raise ApiError(404, "event_not_found", "No such event")
    seats = [
        {"id": r["id"], "label": r["label"], "row_idx": r["row_idx"],
         "col_idx": r["col_idx"], "status": r["status"]}
        for r in rows
    ]
    return {
        "event_id": event_id,
        "seats": seats,
        "seats_left": sum(1 for s in seats if s["status"] == "available"),
    }
