# SeatRush

A ticket-booking site for concerts with a **flash sale**: an admin opens booking
at an exact instant, hundreds of people rush the same seats, and **nobody can
ever end up with a seat somebody else already has**.

It is built to demonstrate two things clearly:

1. **Nginx as the gateway.** One container is the single entry point, load
   balancer with failover, rate limiter, microcache, TLS terminator, static web
   server and the source of structured request logs. Every directive has a
   reason to exist *in this app*, and `docs/NGINX.md` proves each one with a
   command.
2. **Correctness under concurrency.** Never oversell a seat, never double-book
   on a retry, and let holds expire on their own.

```
                         ┌──────────────────────────────────────────────────┐
 Browser  ──────────────▶│  Nginx   :8080 (HTTP → 301)   :8443 (HTTPS, TLS) │
 (React SPA)             │                                                  │
                         │   /              → React build (SPA fallback)    │
                         │   /api/events*   → microcache 1–2 s              │
                         │   /api/auth/*    → strict per-IP rate limit      │
                         │   /api/holds*    → per-token limit, never retried│
                         │   /api/*         → upstream api_backend (RR)     │
                         │   /nginx_status  → stub_status                   │
                         └───────────────────────┬──────────────────────────┘
                                                 │  round robin, keepalive
                      ┌──────────────────────────┼──────────────────────────┐
                      ▼                          ▼                          ▼
                   api1:8000                 api2:8000                  api3:8000
                   (FastAPI)                 (FastAPI)                  (FastAPI)
                      └──────────────────────────┼──────────────────────────┘
                                                 ▼
                                        PostgreSQL 16
                               (single source of truth, row locks)
```

**The API replicas are stateless.** Auth is a self-contained JWT verified with a
shared secret; every piece of mutable state lives in Postgres. There are no
in-memory sessions and no in-process caches. That is *why* Nginx can round-robin
freely with no sticky sessions — and it is the first thing to point at when
someone asks why there is no `ip_hash`.

---

## Quickstart

Requires Docker Desktop. Nothing else — no Python, no Node, no certificate step.

```bash
docker compose up --build
```

Then open **https://localhost:8443** and accept the self-signed certificate
(see [Troubleshooting](#troubleshooting)). `http://localhost:8080` redirects
there.

Startup order is enforced by conditions, not sleeps:
`postgres` (healthcheck) → `migrate` (runs schema + seed, exits 0) → `api1/2/3`
→ `nginx`.

### Demo accounts (development only)

| Email | Password | Role |
|---|---|---|
| `demo@seatrush.dev` | `demo123` | user |
| `admin@seatrush.dev` | `admin123` | admin — unlocks `/admin` and `/ops` |

Plus 300 load-test fixtures `lt001@seatrush.dev … lt300@seatrush.dev`.

### Seeded events

| # | Event | State |
|---|---|---|
| 1 | Midnight Static – Live | already on sale (8×12 = 96 seats) |
| 2 | Aurora Drift – World Tour | opens **~2 minutes after startup** — the countdown demo |
| 3 | The Paper Lanterns – Farewell | opens in 7 days (10×20 = 200 seats) |

The seed **refreshes those times on every `docker compose up`**, so event 2 is
always about to open when you demo it. Seats, holds and bookings are never
touched by that refresh.

---

## What to look at

| Page | What it shows |
|---|---|
| `/` | event list, seats left, countdown on the server clock |
| `/events/1` | the seat map — polls every 2.5 s, prints the `X-Cache-Status` of the last poll |
| `/checkout/<hold>` | countdown to `expires_at`, Pay, Release |
| `/tickets` | bookings with codes |
| `/admin` | create an event, live stats, **integrity check**, reset-for-demo |
| `/ops` | cluster health, `stub_status`, round-robin and rate-limit demos, live request log |

---

## Running the tests

### Backend test suite (58 tests)

Runs in a container against the compose Postgres, in its own `seatrush_test`
database:

```bash
docker compose run --rm --no-deps -e DB_POOL_MAX=25 api1 pytest
```

The ones that matter:

| Test | Asserts |
|---|---|
| `test_fifty_users_rush_one_seat_exactly_one_wins` | 50 different users, 1 seat → exactly **1×201, 49×409** |
| `test_rush_on_a_small_venue_never_oversells` | 50 users, 10 seats, 2 each → no seat held twice, no partial claims |
| `test_all_or_nothing_partial_claim_rolls_back` | a 3-seat request that loses 1 seat leaves the other 2 **available** |
| `test_concurrent_duplicates_of_one_key` | 10 simultaneous copies of one `Idempotency-Key` → 1 hold, 10 identical 201s |
| `test_concurrent_confirms_of_one_hold_create_one_booking` | 10 concurrent confirms with *different* keys → 1 booking |
| `test_expired_hold_releases_its_seat_to_another_user` | lazy expiry frees a seat with no sweeper running |
| `test_confirming_an_expired_hold_is_410` | paying a timed-out hold is `410 hold_expired` |
| `test_integrity_no_seat_in_two_bookings` | after a full rush: **0 seats in two bookings** |
| `test_cacheable_endpoints_ignore_the_caller` | guards the promise Nginx's cache depends on |

### Automated verification (63 checks)

```bash
./scripts/verify.sh            # everything, ~6 minutes
./scripts/verify.sh --quick    # skip pytest / failover / load tests, ~40 s
```

Covers `nginx -t`, TLS/ALPN, the redirect, SPA fallback, asset caching, gzip,
all six security headers on every location, the full cache policy table, rate
limits (general / login / per-token), 413, the error envelope, round robin,
JSON logging, `stub_status`, failover + rejoin, a zero-downtime reload under
load, and the three load-test modes.

### Load test

```bash
# through Nginx, from the host (needs: pip install httpx PyJWT)
python loadtest/flashsale.py --mode edge --users 300

# straight at the replicas, inside the compose network — no edge protection
docker compose --profile tools run --rm loadtest --mode direct --users 300

# the microcache proof
python loadtest/flashsale.py --poll --event 1 --viewers 40 --duration 20
```

---

## Measured results

All from runs on 2026-10-22 against `docker compose up --build` on this
machine (12-core Windows laptop, Docker Desktop). Transcripts in `runs/`.

### Flash sale — `--mode direct` (Nginx bypassed, maximum contention)

```
300 users, 10 seats, 1.48 s wall time, 202 hold attempts/s

POST /api/holds        201   9    hold created
                       409 291    lost the race, handled cleanly
POST .../confirm       200   9    booking created

latency (ms)        p50   p95   p99   max
  hold              438   933  1081  1182
  confirm           287  1122  1122  1122

seats in the venue      : 10
seats booked            : 10
rows in booking_seats   : 10
SEATS IN TWO BOOKINGS   : 0     ← the whole point
```

**10 seats, 10 booked, 0 oversold** — with no rate limiter helping.

### Flash sale — `--mode edge` (through Nginx)

```
300 users, 10 seats
  201   7   won seats
  409 130   lost the race cleanly
  429 163   stopped at the edge, never reached a backend
→ 10 seats, 9 booked, 0 oversold
```

Same correctness, plus 163 requests that the backends never had to see.

### Microcache

| Offered load | Polls sent | Served from cache | Reached a FastAPI replica |
|---|---|---|---|
| 40 viewers (16 req/s) | 318 | **318 (100%)** | **33 in 20 s** |
| 200 viewers (80 req/s) | 1 594 | 438 of 438 served (**100%**) ¹ | **22 in 20 s** |

¹ the other 1 156 were rejected by the per-IP rate limiter *before* the cache
was consulted — all 200 simulated viewers share one source IP.

**Backend load stays flat at ~1–2 requests/second no matter how big the crowd
is.** That is the entire argument for a microcache, and the numbers show it:
5× more offered load produced *fewer* backend requests.

Backend-side count (the honest measurement — Nginx does not log background
cache refreshes):

```bash
docker compose logs --since <TIMESTAMP> api1 api2 api3 | grep -c 'GET /api/events/1/seats'
```

### Nginx edge behaviour

| Check | Result |
|---|---|
| Round robin, 30 requests | 10 / 10 / 10 across api1/api2/api3 |
| `docker compose kill api2`, 12 requests | **12 × 200**; two show `upstream_status: "504, 200"` (tried the dead replica, retried, user never knew) |
| api2 restarted + 12 s | back to 10 / 10 / 10 |
| 120 rapid `/api/time` | 75 × 200, 45 × 429 |
| 8 wrong-password logins | `401 401 401 401 429 429 429 429` |
| Per-token limit | token A: 6 through / 6 × 429, while token B **from the same IP at the same instant** got 5/5 through |
| 120 KB body | `413` with our JSON envelope |
| gzip on the seat map | 6 587 → 841 bytes (**7.8×**) |
| Security headers | **6/6** on every location, including the four that add their own headers |
| `nginx -s reload` under 400 req | 0 dropped (155 × 200, 245 × 429 — all *served*) |

---

## Deployment

### Plan A — docker-compose (primary, used in the demo)

Covered by the Quickstart above. Only ports 8080 and 8443 are published;
Postgres and the API replicas are reachable only on the private bridge network.

### Plan B — Render (optional)

Render does not run docker-compose, so Plan B is **one web-service container**
(Nginx + three uvicorn processes on `127.0.0.1:8001/8002/8003`) plus a managed
Postgres. See [`deploy/render/`](deploy/render/) and the walkthrough in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#plan-b--render).

> **Check the current free-tier limits before relying on this.** Free Postgres
> instances on Render have historically expired after a fixed period and come
> with small storage quotas; the free web service sleeps after inactivity and
> has **no shell**, so the failover demo must be done in Plan A. Alternatives
> for the database: Neon, Supabase, or Aiven free tiers.

---

## Repository layout

```
backend/
  app/
    main.py          FastAPI wiring, middleware, error handlers, /api/time
    config.py        every env var, with dev defaults
    db.py            the single asyncpg pool
    errors.py        one error envelope for the whole API
    auth.py          scrypt hashing, JWT, the current_user/current_admin guards
    events.py        public cacheable reads (list, detail, seat map)
    holds.py         ★ the conditional UPDATE, confirm, release
    idempotency.py   ★ exactly-once POSTs, in Postgres
    bookings.py      my tickets
    admin.py         create/reset an event, stats + integrity check
    ops.py           health, active cluster probe, access-log tail
    schema.sql       ★ tables, constraints, and the seat_effective view
    seed.py          schema + fixtures, run by the `migrate` service
  tests/             58 tests: auth, holds, concurrency, idempotency, expiry, ops
nginx/
  nginx.conf         ★ the gateway, heavily commented
  snippets/
    proxy-headers.conf      keepalive, forwarded headers, timeouts
    security-headers.conf   the six headers + the add_header inheritance trap
  Dockerfile         multi-stage: Node builds the SPA, Nginx serves it
frontend/            Vite + React (plain JSX), React Router, plain CSS
loadtest/
  flashsale.py       edge / direct / poll modes
deploy/render/       Plan B
scripts/
  gen-cert.sh        self-signed cert; also the container's start-up hook
  verify.sh          63 automated checks
docs/
  ARCHITECTURE.md    request flows and the hold lifecycle
  NGINX.md           section-by-section walkthrough with proof commands
  INTERVIEW_NOTES.md the 12 trickiest things + Q&A
  interview-guide.html  one self-contained study page
docker-compose.yml
DEMO.md              the 6–8 minute screen-share script
```

★ = the files worth reading first.

---

## Decisions

Every judgement call, and what was deliberately left out.

### Concurrency and data

| Decision | Chosen | Alternatives | Why |
|---|---|---|---|
| Claiming seats | A single conditional `UPDATE … WHERE status='available' OR (hold expired) RETURNING id` | `SELECT … FOR UPDATE` then `UPDATE`; optimistic version column; `SERIALIZABLE` | One round trip, and the check and the write are the *same statement*, so no one can later insert code into the gap. `FOR UPDATE` is the textbook answer and also correct; it buys inspection we do not need. `SERIALIZABLE` would make every hot seat a retry storm. |
| Overselling guard | `UNIQUE (seat_id)` on `booking_seats` | trust the application | Defence in depth. The application logic makes double-booking impossible *in practice*; this constraint makes it impossible **even if that logic has a bug**. |
| Hold expiry | **Lazy only** — a hold is expired when `status='active' AND expires_at < now()` | background sweeper; `pg_cron`; a TTL queue | A sweeper is a whole moving part (a worker, a schedule, its own failures) that buys **no correctness**: every read and the claim itself re-check the condition. Trade-off accepted: stale `holds` rows linger until the seat is reclaimed. `/admin` shows the count honestly. |
| Expiry rule lives in | One SQL view, `seat_effective` | repeating the CASE in each query | Written once, so the seat map, `seats_left` and admin stats can never disagree. |
| Idempotency | `(user_id, key)` primary key, written **in the same transaction as the work** | a separate table write; Redis SETNX | Atomicity: "work done" and "key recorded" become one fact. A concurrent duplicate *blocks* on the primary key, then replays the committed response. |
| Failed requests | Roll back the idempotency row too | keep it | A rejected request must leave no trace, so the client may legitimately retry the same action with the same key once the cause is gone. |
| Response storage | `jsonb` | `json` / `text` | Queryable. Cost: Postgres normalises key order, so a replay is *semantically* identical, not byte-identical. Tests assert deep equality. Byte-exact replay (e.g. to re-sign a webhook) would need `text`. |
| Money | integer cents | float; `numeric` | Floats cannot represent money. `numeric` would be right at scale; cents in an `int` is simpler and exact here. |
| SQL access | raw `asyncpg`, numbered placeholders | SQLAlchemy / an ORM | The locking SQL **is** the project. An ORM would hide it behind `with_for_update()`. Always parameterised, so no injection surface. |
| Password hashing | stdlib `hashlib.scrypt` | bcrypt, argon2 | Memory-hard, and no C toolchain in the image. argon2id in production. |
| Auth | stateless JWT (HS256) | server-side sessions | **This is what makes the replicas interchangeable**, which is what makes plain round robin correct. Cost: a revoked user stays valid until `exp`. |

### Nginx

| Decision | Chosen | Why |
|---|---|---|
| Load balancing | round robin + `zone api_backend 64k` | Stateless replicas need no stickiness. The `zone` is **not optional**: without it each worker keeps its own counter, and on a 12-core machine 30 connections all went to api1. Measured. |
| Health checks | passive (`max_fails` / `fail_timeout`) + an app-level active prober at `/api/ops/cluster` | Open-source Nginx has no `health_check` directive. Rather than pretend otherwise, the app probes its siblings and the ops page says so. |
| Retries | `error timeout http_502 http_503 http_504`, **no `non_idempotent`, no `http_500`** | A retried POST could double-book; a 500 means the request *reached* the app and may have committed. |
| Microcache | 1 s on the seat map, 2 s on the list/detail, with `proxy_cache_lock` | Collapses a poll storm ~400:1. Safe because the map is only a hint — correctness is decided in Postgres, and a stale click yields a clean 409. |
| What is cached | only `/api/events*` | The cache key excludes `Authorization`, so only caller-independent responses qualify. A test enforces this. |
| Rate limiting | three zones: per-IP general, per-IP login (r/**m**), per-**token** booking | A per-IP booking limit is useless behind a NAT and too loose for one scripted laptop. |
| `nodelay` | on, everywhere | Fast-and-sometimes-429 beats uniformly-slow for an interactive sale. |
| 429 / 413 / 503 bodies | hand-written JSON matching the API envelope | So the frontend has exactly **one** error parser. |
| Security headers | one snippet, included in *every* location | `add_header` is not additive across levels: a location that adds one header loses all inherited ones. |
| HSTS | `max-age=300`, HTTPS only, via a `map` on `$scheme` | HSTS is sticky and pins *every* future localhost project to HTTPS. |
| TLS | terminated at Nginx; backends plain HTTP | Cheap backends, no per-replica cert. They learn the scheme from `X-Forwarded-Proto`. |
| Static assets | `Cache-Control: public, max-age=31536000, immutable` **written out in full** | `expires 1y` *plus* `add_header Cache-Control` emits **two** Cache-Control headers. Found and fixed. |
| Request buffering | left **on** (the default) | Turning it off would stream slow clients straight to uvicorn and undo `client_body_timeout`. |

### Deliberately left out

| Not built | Why |
|---|---|
| **Redis** | Nothing needs it. Rate limiting and caching are Nginx's job here; sessions do not exist; the only shared state that matters needs *transactions*, which is Postgres. Adding Redis would mean a second source of truth for seat state — the exact thing that causes overselling. |
| **WebSockets / live seat updates** | Polling plus a 1-second microcache gives near-live updates with no connection state, no reconnect logic and no fan-out problem. A WebSocket seat map would need Redis pub/sub to broadcast across three replicas, which is a genuinely bigger system. It is the first thing I would add at scale — see INTERVIEW_NOTES. |
| **Background workers / schedulers** | The only candidate job was sweeping expired holds, and lazy expiry makes it unnecessary for correctness. |
| **Message queue** | No work is deferred. Booking is synchronous and must be. |
| **An ORM** | See above — the SQL is the point. |
| **Kubernetes / cloud IaC** | Out of scope for a demo you can run on a laptop. |
| **`auth_request` JWT validation at the edge** | Would let Nginx rate-limit on a short user id instead of a whole JWT, and reject unauthenticated requests without touching Python. Deliberately left as a stretch goal. |

### Project-level

| Decision | Note |
|---|---|
| Repo root is the project root | Built directly in `SeatRush/` rather than a nested `seatrush/` directory. |
| Container names prefixed `seatrush-` | Lets this stack coexist with other compose projects. Stop a replica with `docker compose stop api2` or `docker stop seatrush-api2`. |
| No `restart:` policy on the API services | So `docker compose stop api2` *stays* stopped during the failover demo. Production would use `unless-stopped`. |
| Tests ship inside the API image | One image, so `docker compose run --rm api1 pytest` needs no second build. |
| `GET /api/admin/loadtest-users` | Demo tooling: the load test needs real user ids to mint JWTs, and logging 300 users in would hit the login rate limit. Scoped to `lt%` fixtures, returns no hashes, would not ship in production. |
| Frontend session in `localStorage` | Simpler than cookies, and cookies would force CSRF handling *and* stop Nginx caching any response carrying `Set-Cookie`. Trade-off: XSS could read the token — mitigated, not eliminated, by the strict same-origin CSP. |

---

## Troubleshooting

**The browser warns about the certificate.**
Expected — it is self-signed, generated on first container start. Click
*Advanced → Proceed to localhost*. In Chrome you can also type `thisisunsafe`
on the warning page. To regenerate: `docker compose down -v` (this also wipes
the database) or delete the `seatrush_nginx_certs` volume.

**Port 8080 or 8443 already in use.**
Something else is bound to them — often another compose project. Find it with
`docker ps` and stop it, or change the host side of the mapping in
`docker-compose.yml` (`"9443:443"`). If you change 8443 you must also update the
redirect target in `nginx.conf`, because the non-standard port is written into
it explicitly.

**`host not found in upstream "api1:8000"` and Nginx will not start.**
Nginx resolves upstream hostnames *at config-parse time*. The compose file makes
`nginx` depend on the three API services for exactly this reason; if you start
Nginx alone, start the replicas first.

**Docker runs out of memory.**
Six containers plus two image builds. Give Docker Desktop at least 4 GB
(Settings → Resources). The Node build stage is the heaviest part and only runs
when the frontend changes.

**`docker compose up` seems to hang on `migrate`.**
It is waiting for the Postgres healthcheck (`pg_isready`). First run also
initialises the database cluster. Give it ~30 s; `docker compose logs postgres`
shows progress.

### Windows / PowerShell differences

The commands in these docs are written for **Git Bash**. In PowerShell:

| Git Bash | PowerShell |
|---|---|
| `./scripts/verify.sh` | `bash scripts/verify.sh` (needs Git Bash installed) |
| `for i in $(seq 1 30); do …; done` | `1..30 \| ForEach-Object { … }` |
| `curl -sk …` | `curl.exe -sk …` — bare `curl` is an alias for `Invoke-WebRequest` |
| `export FOO=bar` | `$env:FOO = "bar"` |
| `grep` / `sed` | `Select-String` / `-replace` |

Two Git Bash gotchas the scripts work around, both of which cost me time:

- Git Bash rewrites Unix-looking **arguments** into Windows paths, so a
  container path like `/var/log/nginx/access.json.log` becomes
  `C:/Program Files/Git/var/log/...`. Fix: `MSYS_NO_PATHCONV=1` or a doubled
  slash (`//var/log/...`).
- With `MSYS_NO_PATHCONV=1` set, `/dev/null` is **no longer** rewritten to `NUL`
  either, so native `curl.exe` tries to create a file at that path, fails, and
  silently reports `size_download=0`. `scripts/verify.sh` picks the null device
  per platform.

---

## Documentation

| Document | For |
|---|---|
| [DEMO.md](DEMO.md) | the 6–8 minute screen-share script, step by step |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | request flows, the hold lifecycle, why each component exists |
| [docs/NGINX.md](docs/NGINX.md) | the config section by section, with the command that proves each feature |
| [docs/INTERVIEW_NOTES.md](docs/INTERVIEW_NOTES.md) | the 12 trickiest things, 15 likely questions, honest limitations |
| [docs/interview-guide.html](docs/interview-guide.html) | one self-contained offline study page |
