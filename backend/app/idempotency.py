"""Exactly-once POSTs, implemented in Postgres.

THE PROBLEM
    A user taps "Hold seats", the response is lost to a flaky network, the
    client retries. Without protection the user now holds two sets of seats --
    or worse, is charged twice. Retries are unavoidable in a distributed
    system, so the server must be able to recognise one.

THE CONTRACT
    The client generates an Idempotency-Key (crypto.randomUUID()) once per
    *user action* and reuses it for every retry of that action. Same key +
    same body  -> the original response, replayed, work done once.
    Same key + different body -> 422 idempotency_mismatch (a key is a receipt
    for one specific request; reusing it for a different one is a client bug).

WHY IT RUNS IN THE SAME TRANSACTION AS THE WORK
    If the key were recorded in its own transaction, there would be a window
    where the key says "done" but the booking has not committed (or the booking
    committed and the key did not). Putting both in one transaction makes
    "work done" and "key recorded" a single atomic fact. Either both are
    visible or neither is.

WHY IT IS SAFE UNDER *CONCURRENT* DUPLICATES (the subtle part)
    Two replicas get the same key at the same instant -- a double-click routed
    by Nginx round robin to api1 and api2. Both run:

        INSERT INTO idempotency_keys (...) ON CONFLICT DO NOTHING RETURNING ...

    Postgres takes a speculative-insertion lock on the primary key
    (user_id, key). The second inserter *blocks* on that lock until the first
    transaction finishes -- ON CONFLICT DO NOTHING does not return early on an
    in-flight conflicting row, it waits. Then:
      - first transaction COMMITTED -> second sees the conflict, inserts
        nothing, returns no row. Because we run at READ COMMITTED, our very
        next statement takes a fresh snapshot and therefore *can* see the
        committed row, response body included. We replay it.
      - first transaction ROLLED BACK (e.g. it hit 409 seat_taken) -> there is
        no row, our insert succeeds, and we legitimately do the work.
    So the work happens exactly once, and the loser waits rather than racing.

HOW IT COMPLEMENTS NGINX
    Nginx is configured NOT to retry POSTs (no `non_idempotent` in
    proxy_next_upstream), so the edge never manufactures a duplicate booking on
    its own. This layer handles the duplicates the edge cannot prevent: the
    user's own double-click and the browser's own retry.
"""

import hashlib
import json

import asyncpg
from fastapi import Header

from .errors import ApiError

# Longest key we will store. Bounded so a client cannot push megabytes into the
# primary key index.
MAX_KEY_LEN = 200


async def require_key(idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    """FastAPI dependency: the header is mandatory on POSTs that create things.

    Making it required (rather than optional-but-honoured) means the server can
    never be put in a state where a retry is ambiguous. A client that forgets
    the header gets a 400 at the door instead of a double booking later.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise ApiError(400, "idempotency_key_required",
                       "Idempotency-Key header is required for this request")
    key = idempotency_key.strip()
    if len(key) > MAX_KEY_LEN:
        raise ApiError(400, "idempotency_key_invalid",
                       f"Idempotency-Key must be at most {MAX_KEY_LEN} characters")
    return key


def request_hash(payload: dict) -> str:
    """Canonical sha256 of the request body.

    sort_keys makes {"a":1,"b":2} and {"b":2,"a":1} hash the same, so key
    ordering in the client's JSON never looks like a different request.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def claim(con: asyncpg.Connection, user_id: int, key: str,
                endpoint: str, payload: dict) -> tuple[bool, int | None, dict | None]:
    """Try to become the one caller that does the work.

    Must be called INSIDE the caller's transaction.

    Returns (claimed, status_code, response_body):
      (True,  None, None)        -> you own this key, do the work, then store()
      (False, code, body)        -> replay: return this stored response as-is
    """
    rhash = request_hash(payload)

    inserted = await con.fetchrow(
        """
        INSERT INTO idempotency_keys (user_id, key, endpoint, request_hash)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, key) DO NOTHING
        RETURNING user_id
        """,
        user_id, key, endpoint, rhash,
    )
    if inserted is not None:
        return True, None, None

    # A row already exists (and, if a sibling transaction was in flight, the
    # INSERT above already waited for it to finish). Fresh snapshot, so this
    # SELECT sees whatever that transaction committed.
    row = await con.fetchrow(
        """
        SELECT endpoint, request_hash, status_code, response_body
        FROM idempotency_keys
        WHERE user_id = $1 AND key = $2
        """,
        user_id, key,
    )
    if row is None:
        # Unreachable in practice: the conflicting transaction rolled back, so
        # our INSERT would have succeeded. Fail loudly rather than silently
        # doing the work twice.
        raise ApiError(409, "idempotency_conflict",
                       "Could not resolve the idempotency key, please retry")

    if row["request_hash"] != rhash or row["endpoint"] != endpoint:
        raise ApiError(422, "idempotency_mismatch",
                       "This Idempotency-Key was already used for a different request")

    # NOTE ON jsonb AND KEY ORDER
    #   response_body is jsonb, which stores a parsed binary form -- so Postgres
    #   does NOT preserve the original key order (it sorts by key length, then
    #   alphabetically) and drops insignificant whitespace. The replayed
    #   response is therefore semantically identical but not byte-identical to
    #   the original, which is why the test asserts deep equality on parsed
    #   JSON rather than comparing strings. JSON object key order carries no
    #   meaning, so no client can legitimately depend on it.
    #   (If byte-exact replay were a requirement -- e.g. to re-sign a webhook
    #   payload -- the column would have to be `json` or plain `text`, which
    #   preserve the input verbatim at the cost of being slower to query.)
    if row["status_code"] is None or row["response_body"] is None:
        # Also unreachable: the response is written in the same transaction
        # that inserted the key, so a visible row always has a body.
        raise ApiError(409, "idempotency_in_progress",
                       "The original request is still being processed, please retry")

    return False, row["status_code"], json.loads(row["response_body"])


async def store(con: asyncpg.Connection, user_id: int, key: str,
                status_code: int, body: dict) -> None:
    """Record the response on the row we claimed. Same transaction as the work."""
    await con.execute(
        """
        UPDATE idempotency_keys
           SET status_code = $3, response_body = $4
         WHERE user_id = $1 AND key = $2
        """,
        user_id, key, status_code,
        json.dumps(body, default=str),
    )
