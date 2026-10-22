# ARCHITECTURE.md

How a request actually moves through SeatRush, what each component is for, and
the full lifecycle of a seat hold.

---

## 1. The components, and why each one exists

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
                                                 │  round robin + keepalive
                      ┌──────────────────────────┼──────────────────────────┐
                      ▼                          ▼                          ▼
                   api1:8000                 api2:8000                  api3:8000
                   FastAPI                   FastAPI                    FastAPI
                   uvicorn                   uvicorn                    uvicorn
                      └──────────────────────────┼──────────────────────────┘
                                                 │  asyncpg pool (10/replica)
                                                 ▼
                                        PostgreSQL 16
                               single source of truth · row locks
```

| Component | Why it exists | What happens without it |
|---|---|---|
| **Nginx** | One entry point. Terminates TLS, serves the SPA, balances three replicas, survives one dying, rate-limits, and absorbs the seat-map polling. | Each replica needs its own certificate and public port; the browser needs to know about three hosts; a poll storm hits Postgres directly; brute-forcing login is free. |
| **api1/2/3 (FastAPI)** | Business rules and the SQL that enforces them. Three replicas so the system survives one crashing and so load balancing is demonstrable. | One replica = one point of failure, and nothing to load balance. |
| **PostgreSQL** | The **only** place seat ownership is decided. Row locks + transactions are the actual oversell guard. | Nothing else in the stack can make "exactly one owner per seat" true. |
| **migrate (one-shot)** | Applies `schema.sql` and seeds fixtures, then exits. Replicas wait for it. | The first replica to boot races the migration and crashes on a missing table. |
| **React SPA** | The UI. Served as static files by Nginx from the same origin as `/api`. | — |

### Why the replicas are stateless, and why it matters

Each replica holds **no mutable state**: no session store, no in-process cache,
no sticky anything. Authentication is a self-contained JWT verified with a
shared secret, and everything else — seats, holds, bookings, idempotency keys —
lives in Postgres.

Three consequences follow from that one property, and they are the backbone of
the whole design:

1. **Any replica can serve any request from any user**, so Nginx can use plain
   round robin. No `ip_hash`, no session affinity.
2. **A replica can die mid-session with no user-visible loss**, so passive
   failover plus `proxy_next_upstream` is enough.
3. **Scaling out is adding a line to the `upstream` block**, not a
   re-architecture.

The cost is that every authenticated request carries a ~160-byte JWT, and a
revoked user stays valid until `exp`. Both are acceptable here; the fix for the
second (a deny-list) is in the limitations.

---

## 2. Request flow — loading the seat map

The highest-volume request in the system. Every open tab repeats it every
2.5 seconds.

### 2a. Cache MISS (the first viewer, or once per second)

```
 Browser                Nginx                         api2                Postgres
    │                     │                             │                     │
    │ GET /api/events/1/seats                           │                     │
    ├────────────────────▶│                             │                     │
    │                     │ 1. location ~ ^/api/events/[0-9]+/seats$          │
    │                     │ 2. limit_req api_ip → under the limit, proceed    │
    │                     │ 3. proxy_cache lookup → MISS                      │
    │                     │ 4. proxy_cache_lock: I own the refill             │
    │                     │ 5. pick a replica (round robin, shared zone)      │
    │                     │    reuse a pooled keepalive connection            │
    │                     ├────────────────────────────▶│                     │
    │                     │                             │ SELECT … FROM       │
    │                     │                             │ seat_effective      │
    │                     │                             ├────────────────────▶│
    │                     │                             │◀────────────────────┤
    │                     │◀────────────────────────────┤  JSON, ~6.5 KB      │
    │                     │ 6. store in cache, valid 1 s                      │
    │                     │ 7. gzip → ~840 bytes                              │
    │                     │ 8. add security headers + X-Cache-Status: MISS    │
    │◀────────────────────┤                             │                     │
    │  200, 841 bytes     │                             │                     │
```

**Who does what:**

| Step | Directive / code |
|---|---|
| 1 | `location ~ ^/api/events/[0-9]+/seats$` — a regex, checked before the `/api/` prefix |
| 2 | `limit_req zone=api_ip burst=40 nodelay` |
| 3–4 | `proxy_cache api_cache` · `proxy_cache_valid 200 1s` · `proxy_cache_lock on` |
| 5 | `upstream api_backend` + `zone api_backend 64k` · `keepalive 32` + `proxy_http_version 1.1` |
| — | `backend/app/events.py:get_seats` → the `seat_effective` view in `schema.sql` |
| 7 | `gzip on` · `gzip_types application/json` |
| 8 | `snippets/security-headers.conf` · `add_header X-Cache-Status $upstream_cache_status always` |

### 2b. Cache HIT (everybody else)

```
 Browser                Nginx
    │ GET /api/events/1/seats
    ├────────────────────▶│  location → limit_req → cache lookup → HIT
    │◀────────────────────┤  200, X-Cache-Status: HIT
         ~2.5 ms          │
                          └── no backend involved. No Postgres query.
```

Nothing past Nginx is touched. In the access log this shows as an **empty**
`upstream_addr`, which is the cleanest possible evidence.

**The thundering-herd detail:** when the 1-second entry expires, *all* waiting
pollers would normally be declared misses simultaneously and forwarded at once —
a stampede every second, worse than no cache. `proxy_cache_lock on` lets exactly
one through to refill; `proxy_cache_use_stale updating` +
`proxy_cache_background_update on` serve the slightly stale copy to everyone else
immediately, so nobody waits for the refill.

**Measured:** 318 polls in 20 s → 100% served from cache, 33 requests reaching a
replica. At 1 594 polls the backend saw **22**. The backend's load does not
depend on the crowd size.

---

## 3. Request flow — holding seats (the concurrency core)

```
 Browser               Nginx                    api1                  Postgres
    │                    │                        │                       │
    │ POST /api/holds                             │                       │
    │ Authorization: Bearer <jwt>                 │                       │
    │ Idempotency-Key: <uuid v4>                  │                       │
    │ {"event_id":1,"seat_ids":[5,6]}             │                       │
    ├───────────────────▶│                        │                       │
    │                    │ location /api/holds                            │
    │                    │ limit_req booking_user (per TOKEN) 2r/s burst 5│
    │                    │ limit_req api_ip                 20r/s burst 40│
    │                    │ limit_conn conn_ip 50                          │
    │                    │ NOT cached · NOT retried (no non_idempotent)   │
    │                    ├───────────────────────▶│                       │
    │                    │                        │  BEGIN ───────────────▶
    │                    │                        │                       │
    │                    │   ① INSERT idempotency_keys ON CONFLICT DO NOTHING
    │                    │      → no row back? replay the stored response and stop
    │                    │   ② SELECT … (now() >= sale_opens_at)  → else 403
    │                    │   ③ seats exist for this event?        → else 404
    │                    │   ④ user already holds seats here?     → else 409
    │                    │   ⑤ INSERT holds (expires_at = now() + 300s)
    │                    │   ⑥ UPDATE seats … RETURNING id      ← THE CLAIM
    │                    │   ⑦ got fewer than asked? → raise → ROLLBACK → 409
    │                    │   ⑧ store the response on the idempotency row
    │                    │                        │  COMMIT ──────────────▶
    │                    │◀───────────────────────┤                       │
    │◀───────────────────┤ 201 {hold_id, expires_at, total_cents}         │
```

**All of ① to ⑧ is ONE transaction.** Any raise unwinds it and Postgres rolls
back everything, including the idempotency row — which is deliberate: a rejected
request must leave no trace so the client may retry the same action with the
same key.

### The claim itself

`backend/app/holds.py` → `CLAIM_SEATS_SQL`:

```sql
UPDATE seats
   SET status = 'held', hold_id = $1
 WHERE event_id = $2
   AND id = ANY($3::bigint[])
   AND (status = 'available'
        OR (status = 'held' AND hold_id IN (
              SELECT id FROM holds
               WHERE status = 'active' AND expires_at < now())))
RETURNING id
```

### Two transactions racing for seat 5

| | T1 (Alice) | T2 (Bob) | Seat 5 |
|---|---|---|---|
| t0 | `BEGIN` | `BEGIN` | `available` |
| t1 | `UPDATE … id = 5` → takes the **row lock**, writes `held` | | `available` (uncommitted change pending) |
| t2 | | `UPDATE … id = 5` → finds the row write-locked → **BLOCKS** | |
| t3 | `RETURNING` → `{5}`, count matches | *(still blocked — queued on that one tuple, not spinning)* | |
| t4 | `COMMIT` | | `held` by Alice |
| t5 | | wakes up. At READ COMMITTED, Postgres re-reads the **new** row version and **re-evaluates the WHERE clause** against it (the EvalPlanQual recheck) | `held`, live hold |
| t6 | | the row no longer matches → **0 rows returned** | |
| t7 | | `len(got)=0 ≠ 1` → raise → `ROLLBACK` → **409 `seat_taken`** | `held` by Alice |

**The re-evaluation at t5 is the crux.** A naive `SELECT` then `UPDATE` would
read "available" at t1, block on nothing, and both writers would commit — the
classic lost update. Here the check and the write are a single statement, so
there is no window between them.

**All-or-nothing:** if Bob had asked for seats 4, 5 and 6, the rollback at t7
also releases 4 and 6, which he had briefly won. A half-claim never becomes
visible. (`test_all_or_nothing_partial_claim_rolls_back`)

**Deadlocks:** two transactions can lock the same seats in opposite orders.
Postgres detects the cycle and aborts one with `40P01`, which from the client's
point of view is indistinguishable from losing the race — so `claim_seats`
catches `DeadlockDetectedError` and returns the same 409.

---

## 4. Request flow — confirming payment

```
 POST /api/holds/<id>/confirm      Idempotency-Key: <uuid>
      │
      ├── Nginx: per-token limit, no cache, no retry
      ▼
    BEGIN
      ① idempotency claim — a duplicate replays and stops here
      ② SELECT … FROM holds WHERE id = $1 FOR UPDATE OF h
         ├─ not yours / missing     → 404
         ├─ already 'confirmed'     → return the existing booking (200)
         ├─ 'released'              → 410 hold_released
         └─ expires_at < now()      → 410 hold_expired
      ③ SELECT seats WHERE hold_id = $1 AND status = 'held'
      ④ INSERT bookings (booking_code 'SR-7K2Q9X', total_cents)
      ⑤ INSERT booking_seats  ← UNIQUE (seat_id) is the last line of defence
      ⑥ UPDATE seats  SET status = 'booked'
      ⑦ UPDATE holds  SET status = 'confirmed'
      ⑧ store the response
    COMMIT
```

**Why `FOR UPDATE` here but a conditional `UPDATE` for the claim?** Because
confirm must *read several fields and then do multi-table work* while holding the
row. Locking the hold serialises two concurrent confirms: the second waits, then
sees `status='confirmed'` and returns the same booking rather than creating a
second one. (`test_concurrent_confirms_of_one_hold_create_one_booking` — 10
concurrent confirms with 10 *different* keys → 1 booking.)

**Step ⑤ is the backstop.** `UNIQUE (seat_id)` on `booking_seats` means a seat
can appear in at most one booking *ever*. If the claim logic in ⑥ were ever
broken by a future edit, this INSERT fails, the transaction rolls back, and the
user gets an ordinary 409 — the database refuses to oversell even when the
application tries to.

---

## 5. Request flow — a request while api2 is down

```
 Browser          Nginx                         api2 (DEAD)      api3
    │ GET /api/health
    ├───────────▶│
    │            │ round robin picks api2 (shared zone)
    │            ├────────────────────────────▶│  no answer
    │            │   … proxy_connect_timeout 3s …
    │            │ ✗ attempt 1 failed (504)
    │            │
    │            │ proxy_next_upstream: error timeout http_502/503/504
    │            │   → this is a GET, so retrying is safe
    │            ├──────────────────────────────────────────────▶│
    │            │◀──────────────────────────────────────────────┤ 200
    │◀───────────┤ 200  X-Upstream: 172.21.0.4:8000, 172.21.0.5:8000
```

Access log for that one request:

```json
{"status":200,"request_time":3.005,
 "upstream_addr":"172.21.0.4:8000, 172.21.0.5:8000",
 "upstream_status":"504, 200",
 "upstream_response_time":"3.003, 0.002"}
```

After `max_fails=2` failures within `fail_timeout=10s`, api2 is taken out of
rotation for 10 seconds, so only the first couple of requests pay the timeout.
**Measured: 12 requests after a `kill`, all 200, two of them showing the retry.**

**If it had been a POST**, Nginx would **not** retry — by default it only
replays requests it considers safe, and `non_idempotent` is deliberately absent.
The user gets an honest error, and the app's `Idempotency-Key` layer makes their
manual retry safe. The division of labour: *Nginx never manufactures a duplicate;
the app absorbs the duplicates Nginx cannot prevent.*

**Why open-source Nginx needs help here:** it has only **passive** health checks
— it learns a replica is down by failing a real request. The `health_check`
directive is Nginx Plus. So the app ships its own active prober at
`GET /api/ops/cluster`, which fans out to every peer's `/api/health` concurrently
with a 1-second timeout, and the `/ops` page renders it.

---

## 6. The hold lifecycle

```
                          ┌───────────────┐
                          │   available   │◀───────────────────────┐
                          └───────┬───────┘                        │
                                  │                                │
                POST /api/holds   │  conditional UPDATE wins       │
                                  ▼                                │
                          ┌───────────────┐                        │
                 ┌────────│     held      │────────┐               │
                 │        │  hold_id → H  │        │               │
                 │        └───────┬───────┘        │               │
                 │                │                │               │
  DELETE /api/holds/H             │   POST /api/holds/H/confirm    │
  hold → 'released'               │   hold → 'confirmed'           │
  seats → available               │   seats → booked               │
                 │                │                │               │
                 │                ▼                ▼               │
                 │        ┌──────────────┐  ┌──────────────┐       │
                 └───────▶│  (released)  │  │    booked    │       │
                          └──────┬───────┘  └──────────────┘       │
                                 │              terminal           │
                                 └──────────────────────────────────┘

       ┌────────────────────────────────────────────────────────────┐
       │  LAZY EXPIRY — no job runs, nothing is rewritten.           │
       │  expires_at < now() AND status = 'active'                   │
       │    ⇒ every reader TREATS the seat as available              │
       │    ⇒ the next claim overwrites hold_id in place             │
       └────────────────────────────────────────────────────────────┘
```

### Why lazy expiry is correct

No query anywhere trusts `seats.status` on its own.

**Reads** go through the `seat_effective` view (`schema.sql`), which is the
single place the rule is written:

```sql
CASE WHEN s.status = 'held'
      AND h.status = 'active'
      AND h.expires_at < now()
     THEN 'available' ELSE s.status END AS status
```

**Writes** re-check the same condition *inside the claim*, under a row lock —
the `OR (status='held' AND hold_id IN (… expired …))` branch of
`CLAIM_SEATS_SQL`. So the decision to reuse an expired seat is made atomically
with taking it, not before.

An un-swept expired hold therefore cannot cause a wrong answer. It can only
leave a stale row in the table.

**What we gave up:** `holds` rows and stale `seats.hold_id` pointers linger until
the seat is claimed again. At venue scale (≤200 seats/event) that is noise, and
`/admin` reports the count (`expired_holds`) rather than hiding it. At much
larger scale I would add a nightly `DELETE FROM holds WHERE status='active' AND
expires_at < now() - interval '1 day'` — for **housekeeping**, not correctness.

**What we gained:** no worker process, no scheduler, no "what if the sweeper is
down" failure mode, and no race between a sweeper and a live claim.

### Timing: the sale gate and the countdown

The gate is `now() >= sale_opens_at`, evaluated **in Postgres**. A client clock
can never open a sale early.

So the countdown must tick against the same clock. The frontend fetches
`/api/time` once, stores `offset = server_epoch_ms − client_mid`, and adds it to
`Date.now()` forever after (`ClockProvider` in `frontend/src/state.jsx`). Without
that, a laptop five minutes fast shows "On sale" while every attempt returns
403 — a bug invisible on a correctly-synced developer machine.

`/api/time` is never cached. A cached clock is a wrong clock.

---

## 7. Data model

```
users ──┬──< holds >──┬── events ──< seats
        │             │                │
        └──< bookings ─┘                │
                 │                      │
                 └──< booking_seats >───┘
                          UNIQUE(seat_id)
        │
        └──< idempotency_keys   PK (user_id, key)
```

| Constraint | Why it exists |
|---|---|
| `booking_seats UNIQUE (seat_id)` | **The last line of defence against overselling.** A seat can appear in at most one booking, ever — enforced by the database, not by the care of whoever edits `holds.py` next. |
| `bookings.hold_id UNIQUE` | One booking per hold. A duplicate confirm that somehow slipped past the idempotency layer still cannot create a second booking. |
| `idempotency_keys PK (user_id, key)` | The concurrency primitive. Two simultaneous inserts of the same key cannot both succeed, so exactly one caller does the work — and the loser *blocks*, then reads the committed response. Scoped per user, so two users may pick the same UUID. |
| `bookings.booking_code UNIQUE` | Codes are typed by humans; collisions must be impossible, not improbable. Insert retries behind a SAVEPOINT. |
| `seats UNIQUE (event_id, label)` | No two "C7"s in one venue. |
| `CHECK (status IN (…))` on every status column | The state machines above are enforced by the schema, not by convention. |
| `events CHECK (rows BETWEEN 1 AND 10)` | Bounds the seat map so one admin typo cannot generate a million rows. |

Money is **integer cents** everywhere. Floats cannot represent money.

---

## 8. Plan B — Render

Render does not run docker-compose, so Plan B collapses the whole topology into
**one container** plus a managed Postgres:

```
  Render edge (TLS)        ┌─────────────── one web service ──────────────┐
      │                    │                                              │
      ▼                    │  Nginx :$PORT                                │
  https://app.onrender.com─┼─▶  /         → React build                   │
                           │    /api/*    → upstream 127.0.0.1:8001-8003  │
                           │                   │     │     │              │
                           │               uvicorn uvicorn uvicorn        │
                           │                api1   api2   api3            │
                           └──────────────────┬───────────────────────────┘
                                              ▼
                                    Render PostgreSQL (DATABASE_URL)
```

Only four things differ from Plan A, and each one has a reason:

| Difference | Why |
|---|---|
| `upstream` points at `127.0.0.1:8001/8002/8003` | One container, so the replicas are processes, not hosts. |
| `listen ${PORT};`, no TLS block, no redirect | **Render terminates HTTPS at its edge** and hands us plain HTTP on `$PORT`. Keeping our own TLS block would be wrong and would not even have a certificate. |
| `envsubst '${PORT}'` — **that one variable only** | `envsubst` with no argument replaces *every* `$name` it sees, which would eat `$host`, `$remote_addr`, `$upstream_addr` and every other **Nginx** variable and produce an empty, broken config. Restricting it to `${PORT}` is what keeps Nginx's own variables intact. |
| `real_ip_header X-Forwarded-For` + `set_real_ip_from <render range>` | Behind Render's proxy, `$remote_addr` is Render's IP, so **every visitor would share one rate-limit bucket**. These make the per-IP zones see real clients. Trade-off: `X-Forwarded-For` is client-supplied, so `set_real_ip_from` must list only the trusted proxy range — otherwise anyone can spoof their way around the limiter. |

`start.sh` runs the schema + seed, starts the three uvicorn processes, **waits
for `/api/health` on each**, and only then execs `nginx -g 'daemon off;'` — in
that order, because Nginx resolves upstreams at startup and would exit if they
were not listening yet.

**Known Plan B limitations:** the free tier sleeps after inactivity (open the
site a few minutes before a demo to absorb the cold start) and gives you **no
shell**, so the failover demo must be done in Plan A.

---

## 9. Where to look in the code

| Question | File |
|---|---|
| How is a seat claimed without overselling? | `backend/app/holds.py` → `CLAIM_SEATS_SQL`, `claim_seats` |
| How is a duplicate POST absorbed? | `backend/app/idempotency.py` → `claim`, `store` |
| Where is lazy expiry defined? | `backend/app/schema.sql` → the `seat_effective` view |
| What stops overselling if the code is wrong? | `backend/app/schema.sql` → `UNIQUE (seat_id)` |
| Why can Nginx cache the seat map? | `backend/app/events.py` + `backend/tests/test_edge_contract.py` |
| How does the countdown avoid the client clock? | `frontend/src/state.jsx` → `ClockProvider` |
| Where is one idempotency key per user action? | `frontend/src/state.jsx` → `useAction` |
| Every Nginx decision | `nginx/nginx.conf` + `docs/NGINX.md` |
