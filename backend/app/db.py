"""One asyncpg connection pool per process.

Why a pool and not a connection per request: Postgres backends are processes,
so connections are expensive. Three API replicas x 10 pooled connections = 30
Postgres backends, which is a sane load for a single node.

Why no ORM: the whole point of this project is that the locking SQL is visible
and explainable. Raw SQL with numbered placeholders ($1, $2) is what asyncpg
speaks, and it is always parameterised -- never string-formatted -- so there is
no SQL injection surface.
"""

import asyncpg

from . import config

_pool: asyncpg.Pool | None = None


async def connect() -> asyncpg.Pool:
    """Create the pool. Called once from the FastAPI lifespan hook."""
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=config.DATABASE_URL,
        min_size=2,
        max_size=10,
        # Postgres would happily keep a connection forever; recycling bounds the
        # damage from a leaked/stuck session.
        max_inactive_connection_lifetime=300,
        # Statement cache off keeps us compatible with connection poolers such
        # as PgBouncer in transaction mode (a likely production step).
        statement_cache_size=0,
    )
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    """Accessor used by the route modules. Fails loudly if startup was skipped."""
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    return _pool
