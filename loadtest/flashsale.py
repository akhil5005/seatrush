#!/usr/bin/env python3
"""SeatRush flash-sale load test.

Two things this proves, and they are deliberately separate:

  BOOKING MODES -- "can this oversell?"
    --mode edge     run from the HOST, through Nginx on :8443.
                    Shows the edge doing its job: many 429s, and still zero
                    overselling.
    --mode direct   run INSIDE the compose network, round-robin straight to
                    api1/api2/api3, bypassing Nginx entirely.
                    No rate limiting, so maximum contention lands on Postgres
                    row locks. This is the mode that proves the LOCKING is
                    correct, with no edge protection helping it.

    The point: edge protection and data correctness are different layers, so
    they are tested separately. A test that only ran through the rate limiter
    could pass because the limiter throttled the race away.

  POLL MODE -- "does the microcache actually work?"
    --poll          200 simulated viewers polling the seat map for 20s.
                    Reports the X-Cache-Status distribution and the HIT ratio.

Usage:
    # through Nginx, from the host
    python loadtest/flashsale.py --mode edge --users 300

    # straight at the replicas, from inside the compose network
    docker compose --profile tools run --rm loadtest --mode direct --users 300

    # microcache proof
    python loadtest/flashsale.py --poll
"""

import argparse
import asyncio
import itertools
import os
import random
import sys
import time
import uuid
from collections import Counter

import httpx
import jwt

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@seatrush.dev")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# Must match the API's JWT_SECRET. DEV ONLY -- the whole trick of minting
# tokens locally only works because this is a shared development secret.
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-insecure-jwt-secret-change-me")

EDGE_BASE = os.getenv("EDGE_BASE", "https://localhost:8443")
DIRECT_TARGETS = [
    t.strip() for t in
    os.getenv("DIRECT_TARGETS", "http://api1:8000,http://api2:8000,http://api3:8000").split(",")
    if t.strip()
]


def mint_token(user_id: int, role: str = "user") -> str:
    """A JWT the API will accept, without going through /api/auth/login."""
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "role": role, "iat": now, "exp": now + 3600},
        JWT_SECRET, algorithm="HS256",
    )


def pct(values, p):
    """Nearest-rank percentile. Returns ms."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered))) - 1))
    return ordered[k] * 1000


def banner(title):
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


# --------------------------------------------------------------------------
# Setup helpers
# --------------------------------------------------------------------------

async def admin_token(client: httpx.AsyncClient, base: str) -> str:
    r = await client.post(f"{base}/api/auth/login",
                          json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    if r.status_code == 429:
        raise SystemExit(
            "Admin login was rate limited (429). Nginx allows ~5 logins/minute "
            "per IP. Wait a minute and re-run."
        )
    r.raise_for_status()
    return r.json()["token"]


async def server_now_ms(client: httpx.AsyncClient, base: str) -> int:
    """The DATABASE clock. The sale gate is `now() >= sale_opens_at` evaluated
    in Postgres, so the load test must wait against that clock -- not the
    laptop's, which may be seconds off."""
    r = await client.get(f"{base}/api/time")
    r.raise_for_status()
    return r.json()["epoch_ms"]


async def create_sale_event(client, base, token, seats: int, opens_in_s: float) -> dict:
    """One row of `seats` seats, on sale `opens_in_s` from now."""
    now = time.time()
    body = {
        "title": f"Flash Sale Test {uuid.uuid4().hex[:8]}",
        "venue": "Load Test Arena",
        "starts_at": _iso(now + 30 * 86400),
        "sale_opens_at": _iso(now + opens_in_s),
        "rows": 1,
        "cols": seats,
        "price_cents": 100000,
    }
    r = await client.post(f"{base}/api/admin/events", json=body,
                          headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    created = r.json()

    seat_rows = await client.get(f"{base}/api/events/{created['id']}/seats")
    seat_rows.raise_for_status()
    return {
        "id": created["id"],
        "sale_opens_at": created["sale_opens_at"],
        "seat_ids": [s["id"] for s in seat_rows.json()["seats"]],
    }


def _iso(epoch_seconds: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch_seconds, dt.timezone.utc).isoformat()


async def fetch_user_ids(client, base, token, n: int) -> list[int]:
    r = await client.get(f"{base}/api/admin/loadtest-users?limit={n}",
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    users = r.json()["users"]
    if len(users) < n:
        print(f"  ! only {len(users)} load-test users seeded, asked for {n}")
    return [u["id"] for u in users]


# --------------------------------------------------------------------------
# Booking run
# --------------------------------------------------------------------------

async def run_booking(args) -> int:
    direct = args.mode == "direct"
    base_for_setup = DIRECT_TARGETS[0] if direct else args.base

    # verify=False: the edge uses a self-signed certificate.
    limits = httpx.Limits(max_connections=args.concurrency + 20,
                          max_keepalive_connections=args.concurrency + 20)
    async with httpx.AsyncClient(verify=False, timeout=20.0, limits=limits) as setup:
        banner(f"FLASH SALE -- mode={args.mode}  users={args.users}  seats={args.seats}")
        if direct:
            print(f"  targets      : {', '.join(DIRECT_TARGETS)}  (Nginx BYPASSED)")
        else:
            print(f"  target       : {args.base}  (through Nginx)")

        token = await admin_token(setup, base_for_setup)
        event = await create_sale_event(setup, base_for_setup, token,
                                        args.seats, args.open_in)
        user_ids = await fetch_user_ids(setup, base_for_setup, token, args.users)
        if not user_ids:
            print("  ! no load-test users. Run the migrate service first.")
            return 2

        print(f"  event        : {event['id']}  ({len(event['seat_ids'])} seats)")
        print(f"  sale opens   : {event['sale_opens_at']}")
        print(f"  users        : {len(user_ids)}  concurrency cap {args.concurrency}")

        # Wait for the sale against the SERVER clock.
        opens_ms = _parse_iso_ms(event["sale_opens_at"])
        while True:
            now_ms = await server_now_ms(setup, base_for_setup)
            remaining = (opens_ms - now_ms) / 1000
            if remaining <= 0.05:
                break
            print(f"  waiting {remaining:0.1f}s for the sale to open...")
            await asyncio.sleep(min(remaining, 1.0))

    # ---- the rush ---------------------------------------------------------
    sem = asyncio.Semaphore(args.concurrency)
    hold_status = Counter()
    confirm_status = Counter()
    hold_latency: list[float] = []
    confirm_latency: list[float] = []
    errors: Counter = Counter()

    # One client per target so `direct` mode really does spread across the
    # three replicas (there is no load balancer in that mode -- we are it).
    clients = [
        httpx.AsyncClient(verify=False, timeout=20.0, limits=limits)
        for _ in (DIRECT_TARGETS if direct else [args.base])
    ]
    bases = DIRECT_TARGETS if direct else [args.base]
    rr = itertools.cycle(range(len(bases)))

    rng = random.Random(args.seed)

    async def one_user(user_id: int):
        idx = next(rr)
        client, base = clients[idx], bases[idx]
        headers = {"Authorization": f"Bearer {mint_token(user_id)}"}

        # 1-2 random seats, like a real buyer picking a pair.
        want = rng.sample(event["seat_ids"], k=rng.choice([1, 1, 2]))

        async with sem:
            # A fresh key per user action -- this is NOT a duplicate-request
            # test, every one of these is a genuinely different action.
            t0 = time.perf_counter()
            try:
                r = await client.post(
                    f"{base}/api/holds",
                    json={"event_id": event["id"], "seat_ids": want},
                    headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                )
            except Exception as exc:
                errors[type(exc).__name__] += 1
                return
            hold_latency.append(time.perf_counter() - t0)
            hold_status[r.status_code] += 1

            if r.status_code != 201:
                return

            hold_id = r.json()["hold_id"]
            t1 = time.perf_counter()
            try:
                c = await client.post(
                    f"{base}/api/holds/{hold_id}/confirm",
                    headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                )
            except Exception as exc:
                errors[type(exc).__name__] += 1
                return
            confirm_latency.append(time.perf_counter() - t1)
            confirm_status[c.status_code] += 1

    started = time.perf_counter()
    await asyncio.gather(*(one_user(uid) for uid in user_ids))
    elapsed = time.perf_counter() - started

    for c in clients:
        await c.aclose()

    # ---- results ----------------------------------------------------------
    banner("RESULTS")
    total = sum(hold_status.values())
    print(f"  wall time            : {elapsed:0.2f}s "
          f"({total / elapsed:0.0f} hold attempts/s)")
    print()
    print("  POST /api/holds")
    for code in sorted(hold_status):
        print(f"    {code:<5} {hold_status[code]:>5}   {_explain(code)}")
    if errors:
        print(f"    transport errors: {dict(errors)}")
    print()
    print("  POST /api/holds/{id}/confirm")
    for code in sorted(confirm_status):
        print(f"    {code:<5} {confirm_status[code]:>5}   {_explain(code)}")

    print()
    print("  latency (ms)              p50      p95      p99      max")
    print(f"    hold        {pct(hold_latency, 50):>12.0f} "
          f"{pct(hold_latency, 95):>8.0f} {pct(hold_latency, 99):>8.0f} "
          f"{(max(hold_latency) * 1000 if hold_latency else 0):>8.0f}")
    if confirm_latency:
        print(f"    confirm     {pct(confirm_latency, 50):>12.0f} "
              f"{pct(confirm_latency, 95):>8.0f} {pct(confirm_latency, 99):>8.0f} "
              f"{max(confirm_latency) * 1000:>8.0f}")

    # ---- the only assertion that matters ----------------------------------
    async with httpx.AsyncClient(verify=False, timeout=20.0) as check:
        tok = await admin_token(check, base_for_setup)
        s = await check.get(f"{base_for_setup}/api/admin/events/{event['id']}/stats",
                            headers={"Authorization": f"Bearer {tok}"})
        s.raise_for_status()
        stats = s.json()

    banner("INTEGRITY")
    print(f"  seats in the venue           : {stats['total_seats']}")
    print(f"  seats booked                 : {stats['booked']}")
    print(f"  rows in booking_seats        : {stats['booked_seat_rows']}")
    print(f"  bookings created             : {stats['bookings']}")
    print(f"  seats still held (live)      : {stats['held']}")
    print(f"  SEATS IN TWO BOOKINGS        : {stats['seats_in_two_bookings']}   <-- must be 0")
    print()

    ok = (stats["seats_in_two_bookings"] == 0
          and stats["booked_seat_rows"] <= stats["total_seats"]
          and stats["booked"] <= stats["total_seats"])

    if ok:
        print(f"  PASS  {stats['total_seats']} seats, {stats['booked']} booked, "
              f"0 oversold.")
        print(f"        {hold_status.get(409, 0)} users lost the race and were told so "
              f"cleanly (409 seat_taken).")
        if hold_status.get(429):
            print(f"        {hold_status[429]} were stopped at the edge by Nginx "
                  f"(429) and never reached a backend.")
    else:
        print("  FAIL  INTEGRITY VIOLATED -- the database oversold seats.")

    print()
    print(f"  (event {event['id']} left in place for inspection; "
          f"reset it from /admin)")
    return 0 if ok else 1


def _explain(code: int) -> str:
    return {
        200: "confirmed -- booking created",
        201: "hold created -- this user won the seats",
        400: "bad request (seat limit / missing idempotency key)",
        401: "unauthorised",
        403: "sale_not_open",
        404: "event or seat not found",
        409: "seat_taken / hold_limit -- lost the race, handled cleanly",
        410: "hold_expired",
        422: "idempotency_mismatch",
        429: "RATE LIMITED BY NGINX -- never reached a backend",
        500: "server error",
        502: "bad gateway",
        503: "upstream unavailable",
    }.get(code, "")


def _parse_iso_ms(iso: str) -> int:
    import datetime as dt
    return int(dt.datetime.fromisoformat(iso).timestamp() * 1000)


# --------------------------------------------------------------------------
# Poll run -- the microcache proof
# --------------------------------------------------------------------------

async def run_poll(args) -> int:
    base = args.base
    banner(f"MICROCACHE -- {args.viewers} viewers polling for {args.duration}s")
    print(f"  target       : {base}")
    print(f"  poll interval: {args.interval}s per viewer")

    async with httpx.AsyncClient(verify=False, timeout=10.0) as setup:
        r = await setup.get(f"{base}/api/events")
        r.raise_for_status()
        events = r.json()["events"]
        if not events:
            print("  ! no events to poll")
            return 2
        event_id = args.event or events[0]["id"]
        seats = len((await setup.get(f"{base}/api/events/{event_id}/seats")).json()["seats"])
    print(f"  event        : {event_id} ({seats} seats)")

    expected = int(args.viewers * (args.duration / args.interval))
    print(f"  expected     : ~{expected} requests "
          f"({args.viewers / args.interval:0.0f} req/s)")
    print()

    statuses = Counter()
    latencies: list[float] = []
    deadline = time.perf_counter() + args.duration

    limits = httpx.Limits(max_connections=args.viewers + 20,
                          max_keepalive_connections=args.viewers + 20)

    async def viewer(client, stagger):
        # Stagger the start so 200 viewers do not all poll on the same tick --
        # a real crowd arrives spread out, and that is what the cache sees.
        await asyncio.sleep(stagger)
        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            try:
                r = await client.get(f"{base}/api/events/{event_id}/seats")
                latencies.append(time.perf_counter() - t0)
                # Exactly ONE bucket per request. A 429 never reaches the cache
                # at all -- Nginx rejects it before proxy_cache is consulted --
                # so it must not also be counted as a cache outcome.
                if r.status_code == 429:
                    statuses["RATE_LIMITED"] += 1
                elif r.status_code != 200:
                    statuses[f"HTTP {r.status_code}"] += 1
                else:
                    statuses[r.headers.get("X-Cache-Status", "(none)")] += 1
            except Exception as exc:
                statuses[type(exc).__name__] += 1
            await asyncio.sleep(args.interval)

    async with httpx.AsyncClient(verify=False, timeout=10.0, limits=limits) as client:
        await asyncio.gather(*(
            viewer(client, i * (args.interval / args.viewers))
            for i in range(args.viewers)
        ))

    total = sum(statuses.values())
    limited = statuses.get("RATE_LIMITED", 0)
    served = total - limited

    banner("CACHE RESULTS")
    print(f"  total polls sent             : {total}")
    print(f"  effective rate               : {total / args.duration:0.0f} req/s")
    print()
    print("  outcome per request")
    for k in sorted(statuses, key=lambda k: -statuses[k]):
        share = statuses[k] / total * 100 if total else 0
        print(f"    {k:<14} {statuses[k]:>6}  {share:5.1f}%  {_cache_note(k)}")

    # HIT / STALE / UPDATING are all served from cache without the client
    # waiting on a backend. MISS / EXPIRED / REVALIDATED did contact one.
    from_cache = sum(statuses[k] for k in ("HIT", "STALE", "UPDATING"))
    to_backend = sum(statuses[k] for k in ("MISS", "EXPIRED", "REVALIDATED", "BYPASS"))
    ratio = from_cache / served * 100 if served else 0

    print()
    if limited:
        print(f"  stopped at the edge (429)    : {limited}")
        print(f"    ^ EXPECTED, and not a cache result. All {args.viewers} simulated")
        print(f"      viewers share ONE source IP, and the api_ip zone allows")
        print(f"      20r/s burst 40. Offering {total / args.duration:0.0f} req/s from a single IP means")
        print(f"      the rate limiter rejects the excess BEFORE proxy_cache is")
        print(f"      consulted. Real viewers arrive from different IPs. Run with")
        print(f"      --viewers 40 to stay under the limit and see the cache alone.")
        print()
    print(f"  requests actually served     : {served}")
    print(f"    served from cache          : {from_cache}  ({ratio:0.1f}% of served)")
    print(f"    client waited on a backend : {to_backend}")
    print(f"  latency p50/p95 (ms)         : "
          f"{pct(latencies, 50):0.1f} / {pct(latencies, 95):0.1f}")
    print()
    print(f"  Without the microcache every one of those {total} polls would have")
    print(f"  hit a FastAPI replica and run a Postgres query. The 1-second")
    print(f"  proxy_cache_valid plus proxy_cache_lock collapses them to roughly")
    print(f"  one backend request per second per event.")
    print()
    print("  To count what ACTUALLY reached a FastAPI replica -- including the")
    print("  background revalidations no client ever waited for -- count it on the")
    print("  BACKEND side. (Nginx does not write an access-log line for a")
    print("  background cache refresh, so counting its log undercounts.) Take a")
    print("  timestamp before the run, then:")
    print()
    print("    docker compose logs --since <TIMESTAMP> api1 api2 api3 \\")
    print(f"      | grep -c 'GET /api/events/{event_id}/seats'")
    print()
    print("  Measured on this project: 20s of polling reached a replica ~22-33")
    print("  times, i.e. ~1-2 requests/second -- and that number stays FLAT")
    print("  whether 40 or 200 viewers are polling. That is the whole point: the")
    print("  backend's load stops depending on the size of the crowd.")

    # A sane floor: if fewer than half the polls were served from cache,
    # something is wrong with the cache configuration.
    if served and ratio < 90:
        print()
        print(f"  FAIL  only {ratio:0.1f}% of SERVED requests came from cache "
              f"-- expected >90%.")
        return 1
    print(f"  PASS  {ratio:0.1f}% of served polls answered by Nginx without "
          f"touching a backend.")
    return 0


def _cache_note(key: str) -> str:
    return {
        "HIT": "served from cache, no backend involved",
        "STALE": "served stale while a background refresh ran",
        "UPDATING": "served stale, a refresh was already in flight",
        "MISS": "not in cache -- went to a backend",
        "EXPIRED": "entry aged out -- went to a backend",
        "REVALIDATED": "re-checked with the backend",
        "BYPASS": "cache deliberately skipped",
        "RATE_LIMITED": "429 from Nginx -- rejected before the cache was consulted",
        "(none)": "endpoint is not cached at all",
    }.get(key, "")


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="SeatRush flash-sale load test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--mode", choices=["edge", "direct"], default="edge",
                   help="edge = through Nginx (rate limits apply); "
                        "direct = straight to the replicas (no edge protection)")
    p.add_argument("--poll", action="store_true",
                   help="run the microcache proof instead of a booking rush")
    p.add_argument("--base", default=EDGE_BASE,
                   help=f"edge base URL (default {EDGE_BASE})")
    p.add_argument("--users", type=int, default=300)
    p.add_argument("--seats", type=int, default=10,
                   help="seats in the test event (default 10)")
    p.add_argument("--concurrency", type=int, default=150,
                   help="max in-flight hold attempts")
    p.add_argument("--open-in", type=float, default=5.0,
                   help="seconds until the sale opens (default 5)")
    p.add_argument("--seed", type=int, default=1337)
    # poll-mode options
    p.add_argument("--viewers", type=int, default=200)
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--interval", type=float, default=2.5)
    p.add_argument("--event", type=int, default=None,
                   help="poll mode: which event id to poll")
    args = p.parse_args()

    # httpx warns loudly about verify=False; we know, the cert is self-signed.
    import warnings
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    try:
        import urllib3
        urllib3.disable_warnings()
    except ImportError:
        pass

    if args.poll:
        return asyncio.run(run_poll(args))
    return asyncio.run(run_booking(args))


if __name__ == "__main__":
    sys.exit(main())
