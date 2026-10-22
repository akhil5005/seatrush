"""Infrastructure endpoints that power the /ops dashboard.

This module exists because of a real limitation of open-source Nginx: it has
only *passive* health checks (it notices a server is down when a request to it
fails). There is no `health_check` directive -- that is Nginx Plus. So if we
want a live "is api2 up?" grid, something has to do the active probing, and the
cheapest place is the app itself: each replica pings its siblings.
"""

import asyncio
import json
import os
import re

import httpx
from fastapi import APIRouter, Depends, Query

from . import config, db
from .auth import User, current_admin

router = APIRouter(prefix="/api", tags=["ops"])


@router.get("/health")
async def health():
    """Liveness + readiness in one, unauthenticated.

    Nginx's passive health checks never call this -- they judge a server by
    whether real requests to it fail. This endpoint is for /api/ops/cluster,
    for the compose start-up ordering, and for the ops dashboard's round-robin
    demo (30 requests, counted by the X-Instance response header).

    It touches the database on purpose: an API process that cannot reach
    Postgres is useless, so it should not report itself healthy.
    """
    try:
        await db.pool().fetchval("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "instance": config.INSTANCE_NAME,
        "database": "up" if db_ok else "down",
    }


@router.get("/ops/cluster")
async def cluster(_admin: User = Depends(current_admin)):
    """Actively probe every peer's /api/health, all at once.

    asyncio.gather fires all three probes concurrently, so the whole call costs
    one timeout (~1 s) rather than three. The short timeout is the point: a
    dashboard that hangs for 30 s on a dead node is worse than useless.
    """
    peers = config.PEERS or [f"http://127.0.0.1:8000"]

    async with httpx.AsyncClient(timeout=1.0) as client:
        async def probe(base: str) -> dict:
            url = base.rstrip("/") + "/api/health"
            try:
                resp = await client.get(url)
                body = resp.json()
                return {
                    "target": base,
                    "up": resp.status_code == 200,
                    "instance": body.get("instance"),
                    "database": body.get("database"),
                    "status_code": resp.status_code,
                }
            except Exception as exc:
                # Connection refused / timeout -- exactly what a stopped
                # container looks like, which is the failover demo.
                return {
                    "target": base, "up": False, "instance": None,
                    "database": None, "error": type(exc).__name__,
                }

        results = await asyncio.gather(*(probe(p) for p in peers))

    return {
        "reporter": config.INSTANCE_NAME,
        "peers": list(results),
        "up": sum(1 for r in results if r["up"]),
        "total": len(results),
    }


# --------------------------------------------------------------------------- #
# Nginx access log tail
# --------------------------------------------------------------------------- #

def _tail_lines(path: str, limit: int) -> list[str]:
    """Read the last `limit` lines without loading the file.

    A busy flash sale writes a large access log, so we seek to the end and walk
    backwards in 8 KB blocks until we have enough newlines. This is why the ops
    page can poll every 2 s without any cost that grows with log size.
    """
    block = 8192
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        data = b""
        while end > 0 and data.count(b"\n") <= limit:
            step = min(block, end)
            end -= step
            fh.seek(end)
            data = fh.read(step) + data
    return [ln.decode("utf-8", "replace") for ln in data.splitlines() if ln.strip()][-limit:]


@router.get("/ops/logs")
async def ops_logs(
    limit: int = Query(default=50, ge=1, le=config.OPS_LOG_MAX_LIMIT),
    _admin: User = Depends(current_admin),
):
    """Last N entries of Nginx's JSON access log.

    The log lives on a volume that Nginx mounts read-write and the API
    containers mount READ-ONLY, so there is exactly one writer and no chance of
    an API process corrupting it.

    Two details worth knowing:
      - log_format uses `escape=json`, so each line is already valid JSON and
        we only have to json.loads it. With the default escaping, a user agent
        containing a quote would produce a broken line.
      - the upstream_* fields are strings, not numbers: after a retry Nginx
        writes a comma-separated list ("172.18.0.3:8000, 172.18.0.4:8000").
        Treating them as floats is a bug that only shows up during a failover.
    """
    path = config.NGINX_ACCESS_LOG
    if not os.path.exists(path):
        # Running the API without Nginx in front (e.g. pytest) is normal.
        return {"available": False, "path": path, "entries": [],
                "note": "Nginx access log not mounted in this container"}

    entries = []
    for line in _tail_lines(path, limit):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            # A partially-flushed last line. Skip it rather than 500.
            continue
    return {"available": True, "path": path, "entries": entries}


# --------------------------------------------------------------------------- #
# stub_status passthrough
# --------------------------------------------------------------------------- #

_STUB_RE = re.compile(
    r"Active connections:\s*(?P<active>\d+).*?"
    r"(?P<accepts>\d+)\s+(?P<handled>\d+)\s+(?P<requests>\d+).*?"
    r"Reading:\s*(?P<reading>\d+)\s+Writing:\s*(?P<writing>\d+)\s+Waiting:\s*(?P<waiting>\d+)",
    re.S,
)


def parse_stub_status(text: str) -> dict:
    """Turn stub_status' plain-text output into numbers.

    Kept here (and unit-tested) rather than in the browser so the parsing of
    Nginx's fixed-format page has one home. `accepts` minus `handled` is the
    number of connections Nginx dropped -- non-zero means it hit a resource
    limit, which is the single most useful number on that page.
    """
    m = _STUB_RE.search(text or "")
    if not m:
        return {}
    g = {k: int(v) for k, v in m.groupdict().items()}
    g["dropped"] = g["accepts"] - g["handled"]
    return g
