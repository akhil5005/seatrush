"""Schema + fixtures, run by the one-shot `migrate` compose service.

    python -m app.seed --migrate [--loadtest-users 300]

It must be safe to run on every `docker compose up`, so everything here is
idempotent: schema.sql is all CREATE ... IF NOT EXISTS, users are inserted with
ON CONFLICT DO NOTHING, and demo events are matched by title.

One deliberate exception to "change nothing on re-run": the demo events' times
are refreshed relative to now() on every run. Otherwise the event that is meant
to open "in about 2 minutes" would be two weeks in the past by the time of the
interview, and the countdown demo would be dead. Seats, holds and bookings are
never touched by that refresh.
"""

import argparse
import asyncio
import datetime as dt
import pathlib
import string
import sys

import asyncpg

from . import config
from .auth import hash_password

SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")

# DEV-ONLY credentials. They are printed on startup on purpose -- this is a
# demo fixture set, not a production seed. Nothing here is a real secret.
ADMIN_EMAIL, ADMIN_PASSWORD = "admin@seatrush.dev", "admin123"
DEMO_EMAIL, DEMO_PASSWORD = "demo@seatrush.dev", "demo123"
LOADTEST_PASSWORD = "lt-dev-password"


async def apply_schema(con: asyncpg.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    # Executed as one multi-statement script, which asyncpg wraps in an
    # implicit transaction: either the whole schema applies or none of it does.
    await con.execute(sql)
    print("[migrate] schema applied")


async def upsert_user(con: asyncpg.Connection, email: str, password_hash: str,
                      role: str) -> int:
    row = await con.fetchrow(
        """
        INSERT INTO users (email, password_hash, role)
        VALUES ($1, $2, $3)
        ON CONFLICT (email) DO NOTHING
        RETURNING id
        """,
        email, password_hash, role,
    )
    if row is not None:
        return row["id"]
    return await con.fetchval("SELECT id FROM users WHERE email = $1", email)


def _seat_rows(event_id: int, rows: int, cols: int) -> list[tuple]:
    out = []
    for r in range(rows):
        for c in range(cols):
            out.append((event_id, f"{string.ascii_uppercase[r]}{c + 1}", r, c))
    return out


async def upsert_event(con: asyncpg.Connection, title: str, venue: str,
                       starts_at: dt.datetime, sale_opens_at: dt.datetime,
                       rows: int, cols: int, price_cents: int) -> tuple[int, bool]:
    """Returns (event_id, created). Existing events only get their times refreshed."""
    existing = await con.fetchval("SELECT id FROM events WHERE title = $1", title)
    if existing is not None:
        await con.execute(
            "UPDATE events SET starts_at = $2, sale_opens_at = $3 WHERE id = $1",
            existing, starts_at, sale_opens_at,
        )
        return existing, False

    event_id = await con.fetchval(
        """
        INSERT INTO events (title, venue, starts_at, sale_opens_at, rows, cols, price_cents)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        title, venue, starts_at, sale_opens_at, rows, cols, price_cents,
    )
    await con.executemany(
        """
        INSERT INTO seats (event_id, label, row_idx, col_idx, status)
        VALUES ($1, $2, $3, $4, 'available')
        """,
        _seat_rows(event_id, rows, cols),
    )
    return event_id, True


async def seed(con: asyncpg.Connection, loadtest_users: int) -> None:
    now = dt.datetime.now(dt.timezone.utc)

    await upsert_user(con, ADMIN_EMAIL, hash_password(ADMIN_PASSWORD), "admin")
    await upsert_user(con, DEMO_EMAIL, hash_password(DEMO_PASSWORD), "user")
    print(f"[seed] users: {ADMIN_EMAIL} (admin), {DEMO_EMAIL} (user)")

    if loadtest_users > 0:
        # All load-test accounts share ONE pre-computed scrypt hash. Hashing
        # 300 passwords properly would add ~15 s to every `docker compose up`,
        # and these are throwaway fixtures that the load test authenticates as
        # by minting JWTs locally rather than by logging in. Never do this for
        # real users: one salt for many accounts means one cracked hash cracks
        # them all.
        shared = hash_password(LOADTEST_PASSWORD)
        await con.executemany(
            """
            INSERT INTO users (email, password_hash, role)
            VALUES ($1, $2, 'user')
            ON CONFLICT (email) DO NOTHING
            """,
            [(f"lt{i:03d}@seatrush.dev", shared) for i in range(1, loadtest_users + 1)],
        )
        print(f"[seed] {loadtest_users} load-test users lt001..lt{loadtest_users:03d}@seatrush.dev")

    demo_events = [
        # (title, venue, starts_in, sale_opens_in, rows, cols, price_cents)
        # 1. Already on sale -- the "book a seat end to end" demo.
        ("Midnight Static - Live", "Phoenix Arena, Bengaluru",
         dt.timedelta(days=21), dt.timedelta(seconds=-3600), 8, 12, 149900),
        # 2. Opens ~2 minutes after startup -- the countdown / sale_not_open demo.
        ("Aurora Drift - World Tour", "Coastline Amphitheatre, Chennai",
         dt.timedelta(days=45), dt.timedelta(seconds=120), 6, 10, 249900),
        # 3. Far future -- shows a locked event in the list.
        ("The Paper Lanterns - Farewell", "Grand Hall, Mumbai",
         dt.timedelta(days=90), dt.timedelta(days=7), 10, 20, 89900),
    ]

    for title, venue, starts_in, opens_in, rows, cols, price in demo_events:
        event_id, created = await upsert_event(
            con, title, venue, now + starts_in, now + opens_in, rows, cols, price
        )
        verb = "created" if created else "refreshed times for"
        print(f"[seed] {verb} event {event_id}: {title} ({rows}x{cols} seats, "
              f"sale opens {(now + opens_in).isoformat(timespec='seconds')})")


async def main() -> int:
    parser = argparse.ArgumentParser(description="SeatRush schema + seed")
    parser.add_argument("--migrate", action="store_true",
                        help="apply schema.sql before seeding")
    parser.add_argument("--loadtest-users", type=int, default=300,
                        help="how many ltNNN@seatrush.dev accounts to create (0 to skip)")
    args = parser.parse_args()

    # A plain single connection, not a pool: this process does one job and exits.
    con = await asyncpg.connect(config.DATABASE_URL)
    try:
        if args.migrate:
            await apply_schema(con)
        await seed(con, args.loadtest_users)
    finally:
        await con.close()
    print("[migrate] done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
