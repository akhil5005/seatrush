"""Test harness.

Design choices worth knowing:

* Tests run against a REAL PostgreSQL (the one from docker-compose), in a
  separate database called `seatrush_test`. There is no SQLite and no mocking,
  because the thing under test IS Postgres' row-locking behaviour. A fake
  database would make every concurrency test pass for the wrong reason.

* Requests go through the real FastAPI app in-process via httpx's
  ASGITransport. So routing, dependencies, validation and the error handlers
  are all exercised -- just without a network hop or a running uvicorn.
  Nginx is NOT in the picture here: edge behaviour (rate limits, caching) is
  verified separately with curl in scripts/verify.sh, because it is Nginx's
  job, not the app's.

* The pool is created per test rather than once per session. That keeps every
  asyncpg pool bound to the event loop that uses it, which is the single most
  common source of mysterious "attached to a different loop" failures in
  pytest-asyncio.

Run it with:
    docker compose run --rm --no-deps -e DB_POOL_MAX=25 api1 pytest -q
"""

import asyncio
import os
import pathlib

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Point the app at a throwaway database BEFORE importing anything that reads
# config at import time.
# ---------------------------------------------------------------------------
_BASE_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://seatrush:seatrush@postgres:5432/seatrush"
)
_PREFIX = _BASE_DSN.rsplit("/", 1)[0]
TEST_DSN = f"{_PREFIX}/seatrush_test"
# `postgres` is the maintenance database; you cannot CREATE DATABASE while
# connected to the database you are creating.
MAINT_DSN = f"{_PREFIX}/postgres"

os.environ["DATABASE_URL"] = TEST_DSN
os.environ.setdefault("JWT_SECRET", "test-only-secret")
os.environ.setdefault("DB_POOL_MAX", "25")

import asyncpg  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app import config, db  # noqa: E402
from app.auth import hash_password, make_token  # noqa: E402
from app.main import app  # noqa: E402

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "app" / "schema.sql"

# Every table, child-first, so TRUNCATE between tests never trips a foreign key.
_TABLES = ("booking_seats", "bookings", "idempotency_keys", "seats", "holds",
           "events", "users")


async def _bootstrap() -> None:
    con = await asyncpg.connect(MAINT_DSN)
    try:
        # Drop and recreate so a schema change between runs cannot leave a
        # half-migrated test database behind.
        await con.execute("DROP DATABASE IF EXISTS seatrush_test WITH (FORCE)")
        await con.execute("CREATE DATABASE seatrush_test")
    finally:
        await con.close()

    con = await asyncpg.connect(TEST_DSN)
    try:
        await con.execute(SCHEMA.read_text(encoding="utf-8"))
    finally:
        await con.close()


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create seatrush_test once per session. Synchronous on purpose: it owns
    its own short-lived event loop, so it cannot interfere with the per-test
    loops that pytest-asyncio creates."""
    asyncio.run(_bootstrap())
    yield


@pytest_asyncio.fixture
async def pool():
    """Fresh pool + empty tables for every test."""
    await db.connect()
    async with db.pool().acquire() as con:
        # RESTART IDENTITY so seat ids and event ids start from 1 in every
        # test, which keeps the tests readable.
        await con.execute(
            f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"
        )
    try:
        yield db.pool()
    finally:
        await db.disconnect()


@pytest_asyncio.fixture
async def client(pool):
    """httpx client bound to the real ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://seatrush.test") as c:
        yield c


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def admin_token(pool):
    """An admin JWT, minted directly rather than via POST /api/auth/login.

    Signing the token locally is both faster and more honest: these tests are
    not about the login endpoint (auth has its own tests), and going through
    login on every fixture would make the auth tests' assertions about
    rate-limited / failed logins harder to reason about.
    """
    user_id = await pool.fetchval(
        """
        INSERT INTO users (email, password_hash, role)
        VALUES ('admin@test.local', $1, 'admin') RETURNING id
        """,
        hash_password("admin-pw"),
    )
    return make_token(user_id, "admin")


@pytest_asyncio.fixture
async def user_token(pool):
    user_id = await pool.fetchval(
        """
        INSERT INTO users (email, password_hash, role)
        VALUES ('user@test.local', $1, 'user') RETURNING id
        """,
        hash_password("user-pw"),
    )
    return make_token(user_id, "user")


async def make_users(pool, n: int, prefix: str = "rush") -> list[str]:
    """n distinct users, returned as JWTs. Used by the concurrency test.

    One shared password hash: these are fixtures, and computing n scrypt
    hashes would dominate the test's runtime.
    """
    shared = hash_password("x" * 10)
    rows = await pool.fetch(
        """
        INSERT INTO users (email, password_hash, role)
        SELECT $1 || i || '@test.local', $2, 'user'
          FROM generate_series(1, $3) AS i
        RETURNING id
        """,
        prefix, shared, n,
    )
    return [make_token(r["id"], "user") for r in rows]


async def make_event(pool, rows: int = 2, cols: int = 5,
                     sale_opens_in_seconds: float = -60,
                     price_cents: int = 10000) -> dict:
    """Create an event + its seats directly in SQL.

    Bypassing POST /api/admin/events keeps these fixtures independent of the
    admin endpoint, so a bug there cannot make the concurrency tests fail for
    an unrelated reason. Negative sale_opens_in_seconds => already on sale.
    """
    event_id = await pool.fetchval(
        """
        INSERT INTO events (title, venue, starts_at, sale_opens_at, rows, cols, price_cents)
        VALUES ('Test Show', 'Test Venue',
                now() + interval '30 days',
                now() + make_interval(secs => $1),
                $2, $3, $4)
        RETURNING id
        """,
        float(sale_opens_in_seconds), rows, cols, price_cents,
    )
    await pool.executemany(
        """
        INSERT INTO seats (event_id, label, row_idx, col_idx, status)
        VALUES ($1, $2, $3, $4, 'available')
        """,
        [(event_id, f"{chr(65 + r)}{c + 1}", r, c)
         for r in range(rows) for c in range(cols)],
    )
    seat_ids = [
        r["id"] for r in await pool.fetch(
            "SELECT id FROM seats WHERE event_id = $1 ORDER BY row_idx, col_idx",
            event_id,
        )
    ]
    return {"id": event_id, "seat_ids": seat_ids, "price_cents": price_cents}


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def idem(key: str) -> dict:
    return {"Idempotency-Key": key}
