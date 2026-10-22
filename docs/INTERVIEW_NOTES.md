# INTERVIEW_NOTES.md

Three things: the twelve trickiest parts of this project and where they live,
fifteen questions I expect with answers I can actually give, and an honest list
of what is wrong with it.

Line numbers were checked against the code on 2026-10-22. If you edit a file,
re-grep.

---

## Part 1 — the 12 trickiest things

### 1. Conditional `UPDATE` instead of `SELECT … FOR UPDATE`

**`backend/app/holds.py:59-71`** (the SQL) and **`:72-123`** (the reasoning).

```sql
UPDATE seats SET status = 'held', hold_id = $1
 WHERE event_id = $2 AND id = ANY($3::bigint[])
   AND (status = 'available'
        OR (status = 'held' AND hold_id IN (
              SELECT id FROM holds WHERE status='active' AND expires_at < now())))
RETURNING id
```

Two transactions hit the same seat row. T1 takes the row lock and writes. T2
**blocks on that tuple** — not spinning, queued. When T1 commits, T2 wakes and,
because we are at READ COMMITTED, Postgres re-reads the *new* row version and
**re-evaluates the WHERE clause against it** (the EvalPlanQual recheck). The row
no longer matches, so T2 returns zero rows, the count mismatch raises, and the
whole transaction rolls back → 409.

**The crux is that the check and the write are the same statement**, so there is
no window between them. A `SELECT` then `UPDATE` has that window, and that is
the classic lost update.

Why not `FOR UPDATE`: it is also correct, and it is the textbook answer. One
round trip instead of two, the eligibility rule lives in exactly one place, and
nobody can later insert code into the gap. `FOR UPDATE SKIP LOCKED` would be
actively wrong — it would hand the user a *different* seat than the one they
clicked.

### 2. `UNIQUE (seat_id)` as defence in depth

**`backend/app/schema.sql:76-83`**, fired at **`backend/app/holds.py:383-390`**.

```sql
UNIQUE (seat_id)   -- on booking_seats
```

The application logic makes double-booking impossible *in practice*. This
constraint makes it impossible **even if that logic has a bug**. A seat can
appear in at most one booking, ever, or the INSERT fails and the confirm
transaction rolls back.

`GET /api/admin/events/{id}/stats` (**`admin.py:196`**) counts seats appearing in
more than one booking straight from `booking_seats`, independently of any
application code. It must always be 0 — and because of the constraint, the
database cannot even *store* a non-zero answer. The load test asserts on it.

### 3. Idempotency under concurrent duplicates

**`backend/app/idempotency.py:23-42`** (the explanation), **`:88-152`** (the code).

```sql
INSERT INTO idempotency_keys (user_id, key, endpoint, request_hash)
VALUES ($1,$2,$3,$4) ON CONFLICT (user_id, key) DO NOTHING RETURNING user_id
```

The subtle part: **`ON CONFLICT DO NOTHING` does not return early on an
in-flight conflicting row — it blocks.** Postgres takes a speculative-insertion
lock and waits for the other transaction to finish. Then:

- the other transaction **committed** → we get no row back, and because READ
  COMMITTED takes a fresh snapshot per statement, our next `SELECT` *can* see
  the committed row, response body included. We replay it.
- the other transaction **rolled back** → no row exists, our insert succeeds,
  and we legitimately do the work.

So the work happens exactly once and the loser waits rather than racing.

It runs **inside the same transaction as the business operation**
(`holds.py:148`), which makes "work done" and "key recorded" a single atomic
fact. And a *failed* request rolls the key row back too — deliberate, so a
client may retry the same action with the same key once the cause is gone
(`test_a_failed_request_frees_its_key_for_retry`).

### 4. Lazy expiry, and why there is no sweeper

**`backend/app/schema.sql:101-130`** — the `seat_effective` view.

A hold is expired when `status='active' AND expires_at < now()`. Nothing ever
runs to clean it up.

It is correct because **no query anywhere trusts `seats.status` alone**. Reads
go through the view, which is the single place the rule is written. Writes
re-check the same condition *inside the claim*, under a row lock (the `OR
(status='held' AND hold_id IN (…expired…))` branch). So reusing an expired seat
is decided atomically with taking it.

**What I gave up:** stale `holds` rows and `seats.hold_id` pointers linger until
the seat is reclaimed. `/admin` reports the count (`expired_holds`) rather than
hiding it. At much larger scale I would add a nightly DELETE — for
**housekeeping, not correctness**.

**What I gained:** no worker, no scheduler, no "what if the sweeper is down",
and no race between a sweeper and a live claim.

### 5. Nginx never retries POSTs

**`nginx/nginx.conf:360-384`**.

```nginx
proxy_next_upstream error timeout http_502 http_503 http_504;
```

Two absences are the point:

- **no `non_idempotent`** — by default Nginx only replays requests it considers
  safe. Adding that flag would retry POSTs, and a timed-out-but-actually-
  succeeded `confirm`, replayed, is two bookings for one payment.
- **no `http_500`** — a 500 means the request *reached* the app and may have
  committed something.

The division of labour: **Nginx never manufactures a duplicate; the app
(idempotency keys) absorbs the duplicates Nginx cannot prevent** — the user's
own double-click.

`scripts/verify.sh` greps for `non_idempotent` and fails the build if it ever
appears outside a comment.

### 6. Passive vs active health checks

**`nginx/nginx.conf:329-344`** and **`backend/app/ops.py:48-88`**.

Open-source Nginx has **only passive** health checks. There is no prober; the
`health_check` directive is Nginx Plus. It judges a server by how real traffic
behaves: `max_fails=2 fail_timeout=10s`.

The honest consequence: **the first request after a replica dies is the one that
discovers it.** It is not lost — `proxy_next_upstream` retries it elsewhere —
but somebody pays the 3 s connect timeout. Measured, from the access log:

```json
{"status":200,"request_time":3.005,
 "upstream_addr":"172.21.0.4:8000, 172.21.0.5:8000",
 "upstream_status":"504, 200"}
```

Rather than pretend otherwise, the app ships its own **active** prober at
`GET /api/ops/cluster`, which fans out to every peer's `/api/health`
concurrently with a 1-second timeout, and `/ops` renders it and says why.

### 7. Upstream keepalive needs two extra headers

**`nginx/nginx.conf:346-356`** and **`nginx/snippets/proxy-headers.conf:20-21`**.

```nginx
keepalive 32;                      # upstream block
proxy_http_version 1.1;            # snippet
proxy_set_header Connection "";    # snippet
```

By default Nginx talks to upstreams with **HTTP/1.0** and `Connection: close`.
Without both lines the `keepalive 32` pool is **silently never used** — nothing
errors, you just pay a TCP handshake per request and pile up TIME_WAIT sockets.
At 400 polls/second that is 400 extra connection setups per second.

### 8. Leaky bucket: `rate`, `burst`, `nodelay`

**`nginx/nginx.conf:174-232`**.

Each key gets a bucket draining at `rate`; each request adds a drop. `burst` is
how many extra drops may sit in it — a small waiting room for a spike.
`nodelay` serves those queued drops **immediately** instead of spacing them at
`rate`.

Without `nodelay`, a burst of 40 at 20 r/s means the 40th request waits ~2
seconds — a user staring at a frozen page. With it, all 40 go at once and the
bucket then refuses new requests until it drains. For an interactive flash sale
that is the right trade: **fast-and-sometimes-429 beats uniformly-slow.**

429, not the default 503, because 503 means "the server is broken" and 429 means
"slow down, here is how long" — and client libraries treat them differently.

### 9. Per-token vs per-IP rate limiting

**`nginx/nginx.conf:206-232`** (zone), **`:687-696`** (the location).

```nginx
limit_req_zone $http_authorization zone=booking_user:10m rate=2r/s;
```

A per-IP booking limit is **useless** when 400 students share one college NAT,
and **too loose** when one person scripts 50 attempts from one laptop. Keyed on
the JWT, each user gets their own bucket. Measured: token A got 6 through and 6
× 429 while token B, **from the same IP at the same instant**, got 5/5 through.

Two subtleties:

- `limit_req` is **not additive across levels** — a location that declares any
  `limit_req` inherits **none** from the parent. Both zones are listed
  explicitly so a request must satisfy both.
- A request with **no** `Authorization` header produces an **empty key**, and
  Nginx **does not count empty keys at all**. So anonymous traffic slips past
  this zone — which is exactly why `api_ip` is also applied, and why the backend
  401s an unauthenticated POST regardless.

### 10. Microcache staleness vs correctness

**`nginx/nginx.conf:234-300`** and **`:552-596`**.

Every open tab polls the seat map every 2.5 s. 1,000 viewers ≈ 400 req/s of the
identical question. A **1-second** `proxy_cache_valid` collapses that to ~1
backend request per second per event.

Safe because **the seat map is only ever a hint.** Ownership is decided
exclusively by the conditional UPDATE in Postgres. The worst case: a user clicks
a seat someone just took and gets a clean 409 highlighting it. That is a
handled outcome, not a correctness failure.

`proxy_cache_lock on` is the thundering-herd fix (**`:564-572`**): without it,
the instant the entry expires *all* waiting pollers are declared misses and
forwarded at once — a stampede every second, worse than no cache.

**Why only `/api/events*`:** the default cache key is
`$scheme$proxy_host$request_uri` — **it does not include `Authorization`**. So
only caller-independent responses qualify. `backend/tests/test_edge_contract.py`
enforces that promise, so the cache cannot silently start leaking if someone
makes the seat map user-specific.

**Measured:** 318 polls → 100% served from cache, 33 reaching a replica. At
1,594 polls the backend saw **22**. Backend load is flat regardless of crowd
size.

### 11. The `add_header` inheritance trap

**`nginx/snippets/security-headers.conf:4-22`**.

`add_header` is **not additive across configuration levels**:

- a location with **no** `add_header` inherits the whole set;
- a location that defines **even one** inherits **none**.

So the moment a location adds `X-Cache-Status`, it silently loses every security
header. Four locations here add their own (`/assets/` and `= /index.html` add
`Cache-Control`; the three cached API locations add `X-Cache-Status`), so the
snippet is included in **every** location explicitly. `verify.sh` counts 6/6 on
each.

`always` matters too: without it, a 429 or 502 would carry **no** security
headers — and error pages are what an attacker sees most.

**HSTS via a `map`** (**`nginx.conf:166-170`**, used at
**`security-headers.conf:78`**): Nginx skips an `add_header` whose variable
resolves to an empty string, so one shared snippet can live in both the HTTP and
HTTPS server blocks and emit HSTS only on HTTPS. `max-age` is deliberately 300 s
because HSTS is **sticky** — a long one on localhost pins every future project
you run there to HTTPS, curable only via `chrome://net-internals/#hsts`.

### 12. `envsubst` on Plan B

**`deploy/render/start.sh:23-31`**, template at
**`deploy/render/nginx.render.conf.template`**.

```sh
envsubst '${PORT}' < nginx.render.conf.template > /etc/nginx/nginx.conf
```

**The quoted argument is load-bearing.** `envsubst` with no argument replaces
*every* `$name` it finds — including Nginx's own `$host`, `$remote_addr`,
`$upstream_addr`, `$binary_remote_addr`, `$request_uri`.

I captured the counter-demonstration (`runs/planb-render-smoketest.txt`):

```
rendered correctly:
  limit_req_zone $binary_remote_addr  zone=api_ip:10m   rate=20r/s;

what unrestricted envsubst produces:
  limit_req_zone   zone=api_ip:10m   rate=20r/s;
```

The variable is **gone** — and **the config still parses**, which is what makes
it dangerous. You get a running site where every visitor on earth shares one
rate-limit bucket and the logs have no client IPs. `nginx -t` runs immediately
after as a second net.

---

## Part 2 — 15 questions I expect

<details><summary><strong>1. Walk me through what happens when two people click the same seat at the same millisecond.</strong></summary>

Both requests reach Nginx, get round-robined to different replicas, and both
open a transaction. Both run the same conditional UPDATE against seat 5.

T1 gets the row lock first and writes `held`. T2's UPDATE reaches the same row,
finds it write-locked, and blocks — queued on that tuple. T1 commits. T2 wakes
up and, at READ COMMITTED, Postgres re-reads the new row version and
re-evaluates the WHERE clause against it. `status='held'` with a live hold, so
it no longer matches: zero rows.

T2's returned count is smaller than what it asked for, so we raise, the whole
transaction rolls back — including any other seats T2 had won, which is what
makes a multi-seat hold all-or-nothing — and the user gets 409 `seat_taken` with
the conflicting seat ids so the UI can highlight them.

Proof: 50 concurrent users on one seat → exactly 1 × 201 and 49 × 409, in
`test_fifty_users_rush_one_seat_exactly_one_wins`.
</details>

<details><summary><strong>2. Why not <code>SELECT … FOR UPDATE</code>?</strong></summary>

It also works and it is the textbook answer — I would accept it in review. I
chose the conditional UPDATE because it is one round trip instead of two, the
eligibility rule lives in exactly one place, and there is no gap between the
check and the write for someone to later insert code into.

`FOR UPDATE`'s advantage is inspecting each row before deciding, and that buys
nothing here because the decision is all-or-nothing anyway.

`SKIP LOCKED` would be *wrong*: it silently hands the user a different seat than
the one they clicked. It is the right tool for a job queue, not a seat picker.
</details>

<details><summary><strong>3. What if the application logic is wrong?</strong></summary>

That is what `UNIQUE (seat_id)` on `booking_seats` is for. A seat can appear in
at most one booking ever. If the claim logic were broken by a future edit, the
INSERT fails, the confirm rolls back, and the user gets an ordinary 409 — the
database refuses to oversell even when the application tries to.

And the admin stats endpoint counts duplicates straight from `booking_seats`,
independent of any application code, so the number is visible rather than
assumed.
</details>

<details><summary><strong>4. Why no Redis?</strong></summary>

Nothing here needs it. Rate limiting and caching are Nginx's job in this design;
there are no sessions because auth is a JWT; and the one piece of shared state
that really matters — who owns a seat — needs **transactions**, which means
Postgres.

Putting seat state in Redis would create a second source of truth for the thing
the whole project is about. That is how overselling happens.

I would add Redis the moment I wanted live seat updates — pub/sub to fan a seat
change out to WebSocket connections across replicas — or if rate limits needed
to be shared across multiple Nginx nodes.
</details>

<details><summary><strong>5. Is a 1-second-stale seat map not a correctness problem?</strong></summary>

No, because the map is only a hint. Seat ownership is decided exclusively by the
conditional UPDATE in Postgres. The worst case is that a user sees a free seat,
clicks it, and gets a clean 409 with the seat highlighted and the map refreshed.

I traded one second of visual freshness for roughly a 400× reduction in backend
load, and the invariant "never two owners for one seat" was never involved in
that trade.
</details>

<details><summary><strong>6. How do you know the cache is not serving one user's data to another?</strong></summary>

Two things. First, the cache key is `$scheme$proxy_host$request_uri` — it does
**not** include `Authorization` — so only endpoints whose response does not
depend on the caller may be cached. Only `/api/events*` qualifies: no auth is
read, and the seat map never reveals *who* holds a seat.

Second, that is not just a convention, it is a test:
`test_cacheable_endpoints_ignore_the_caller` fetches each cacheable endpoint
with and without a token and asserts the responses are identical. If someone
makes the seat map user-specific, the test fails before the cache can leak
anything.

`verify.sh` also asserts the inverse — that holds, bookings, auth, admin, ops and
time carry **no** `X-Cache-Status` header at all, because `proxy_cache` is set
nowhere except those three locations.
</details>

<details><summary><strong>7. What happens if a replica dies mid-request?</strong></summary>

For a GET: `proxy_next_upstream` retries it on a healthy replica and the user
sees a 200. I have the log line showing `upstream_status: "504, 200"` — tried the
dead one, timed out after 3 s, succeeded elsewhere.

For a POST: **nothing is retried**, deliberately. The user gets an error. Their
manual retry carries the same `Idempotency-Key`, so if the original had actually
committed they get the original response back instead of a second booking.

After two failures in ten seconds the replica is taken out of rotation, so only
the first couple of requests pay the timeout. Measured: 12 requests after a
`kill`, all 200, two showing the retry.
</details>

<details><summary><strong>8. Why round robin and not <code>least_conn</code> or <code>ip_hash</code>?</strong></summary>

Round robin because the replicas are stateless and the requests are uniformly
fast. `least_conn` earns its keep when request cost varies a lot — some 10 ms,
some 2 s — because round robin would keep feeding a replica already stuck on
slow work. Ours do not.

`ip_hash` is for sticky sessions, which is a constraint you are *forced* into by
stateful backends. It would cost even distribution and pin a whole NATed office
to one replica. We do not need it precisely because auth is a self-contained JWT
and all state is in Postgres.

Both are in the config, commented, with a one-line reason each.
</details>

<details><summary><strong>9. What breaks first at 100× traffic?</strong></summary>

**Postgres write contention on the hot seats** — not Nginx, not the app.

The reads are already handled: the microcache makes backend load independent of
crowd size, so 100× viewers is roughly the same backend load. Nginx itself is
nowhere near its limit (`accepts - handled` is 0).

But writes cannot be cached. 100× the rush means 100× the transactions queuing
on the same few hundred seat rows, and they serialise by construction — that is
the point. The claim is fast (p50 438 ms under 300 concurrent users), but at
30,000 concurrent buyers the lock queue, and then the connection pool, become
the wall.

In order, I would: (1) add read replicas and move the seat map off the primary;
(2) shard by event, since events never interact — that is the natural partition
key and it scales almost linearly; (3) put a virtual waiting room in front of
the sale so admission is throttled rather than requests being rejected; (4) only
then consider pre-reserving seat ranges per worker, which trades some
utilisation for far less contention.

The thing I would *not* do is relax the locking.
</details>

<details><summary><strong>10. How would you add live seat updates?</strong></summary>

**WebSocket + Redis pub/sub.** The structure:

- a WebSocket endpoint per event; the browser subscribes instead of polling;
- when a claim or confirm commits, the replica publishes a small delta
  (`{seat_id, status}`) to a Redis channel keyed on the event;
- every replica subscribes and fans the delta out to its own connected sockets.

Redis is needed because a connection lives on **one** replica, and the write may
land on a **different** one — so there has to be something all three share.

The real costs, which is what the question is actually about: connection state
means Nginx needs `proxy_http_version 1.1` plus `Upgrade`/`Connection` headers
and a much longer `proxy_read_timeout` on that location; load balancing stops
being stateless for those connections; you need reconnect-with-resync on the
client, because a dropped socket means missed deltas and the client must be able
to fall back to a full snapshot; and you have added a component whose failure
mode is "the UI silently stops updating".

Polling plus a microcache gives you 90% of the benefit with none of that, which
is why I started here. I would switch when the seat map needed sub-second
accuracy or when poll traffic genuinely became the bottleneck — and I can show
from the measurements that it is not.
</details>

<details><summary><strong>11. What would you change for production?</strong></summary>

In rough order of importance:

1. **Secrets.** `JWT_SECRET` and the database password are dev defaults in the
   repo, clearly marked. They would come from a secret store, and the JWT secret
   would be rotatable (accept two keys during a rotation window).
2. **Managed Postgres** with automated backups, PITR, and a read replica for the
   seat map.
3. **Real certificates** (ACME/Let's Encrypt), HSTS `max-age` raised to a year
   with `includeSubDomains` and preload.
4. **Remove the demo affordances**: the `X-Upstream` header leaks topology,
   `/nginx_status` needs `allow`/`deny`, `/api/admin/loadtest-users` would not
   ship, and `/admin`'s reset endpoint would need real safeguards.
5. **Observability**: Prometheus + Grafana. I would export the four numbers I
   actually look at — 429 rate by zone, cache hit ratio, `upstream_response_time`
   percentiles, and `accepts - handled` — plus alerts on oversell integrity
   (which should be a flat zero forever) and on expired-hold accumulation.
6. **Multiple Nginx nodes** behind a cloud load balancer. That immediately
   breaks per-node rate limiting, so limits would move to a shared store or to
   the cloud LB.
7. **Active health checks**, via Nginx Plus or by swapping to Envoy/HAProxy.
8. **A real payment integration**, which is where the idempotency work already
   done pays off — that is exactly the shape a payment webhook needs.
9. **Hold-expiry housekeeping** (a nightly DELETE), for table size, not
   correctness.
10. **A token deny-list** so a logout or a compromised account takes effect
    before `exp`.
</details>

<details><summary><strong>12. Why is the countdown using server time?</strong></summary>

Because the sale gate is `now() >= sale_opens_at` evaluated **in Postgres**, so
the UI has to count down to the same clock. The frontend fetches `/api/time`
once, stores `offset = server − client` (charging half the round trip to the
response), and adds it to `Date.now()` forever after.

Without it, a laptop five minutes fast shows "On sale" while every attempt
returns 403 `sale_not_open` — a bug that is completely invisible on a correctly
synced developer machine. And `/api/time` is the one endpoint that must never be
cached: a cached clock is a wrong clock.
</details>

<details><summary><strong>13. Why did round robin not work at first?</strong></summary>

This is my favourite thing I learned on this project. Thirty requests all went
to api1 and it looked completely broken.

The round-robin counter and the `max_fails` failure counters live in **each
worker's private memory**. `worker_processes auto` gave me 12 workers on this
machine, so there were 12 independent counters, each starting at api1 — and
twelve separate browser connections all got api1.

The tell was that on a single reused keepalive connection — one worker — the
exact same config cycled api3, api1, api2 correctly. Per-connection broken,
per-worker fine ⇒ the state is per worker.

The fix is one line: `zone api_backend 64k;` puts it in shared memory. The second
half matters as much as the first: without it, worker 7 marking api2 as down
teaches the other 11 workers nothing.
</details>

<details><summary><strong>14. Your tests pass — how do you know they are testing the right thing?</strong></summary>

A few ways I tried to keep myself honest.

The concurrency tests run against **real Postgres**, never a mock or SQLite,
because the thing under test *is* Postgres' locking behaviour. A fake database
would make them pass for the wrong reason.

The load test has two modes on purpose. `--mode edge` goes through Nginx;
`--mode direct` goes **straight to the replicas, bypassing the rate limiter
entirely**. If I only ran through the edge, the test could pass because the
limiter throttled the race away rather than because the locking is right.

The integrity check is computed from `booking_seats` by a query that shares no
code with the booking path.

And I found real bugs with them — including two in my own verification harness
that were making checks lie: a `grep -c … || echo 0` that emitted two zeros, and
`MSYS_NO_PATHCONV=1` stopping Git Bash rewriting `/dev/null`, so curl silently
reported `size_download=0` and the gzip check read "plain=0 gzip=0" while gzip
was working perfectly. A test that cannot fail is not a test.
</details>

<details><summary><strong>15. How did you use AI tools to build this, and how did you verify the code?</strong></summary>

I used an AI coding assistant heavily — for scaffolding, for the first draft of
the Nginx config, and as something to argue with about trade-offs. I think
that is the normal way to work now, and the interesting question is what I did
to make sure I could stand behind the result.

Three things.

**I made the machine prove every claim.** Nothing in the docs is asserted from
memory. Every number in the README came from a command I ran in that session,
and the transcripts are committed in `runs/`. When the microcache section needed
a backend-contact count, I did not trust the Nginx access log — background cache
refreshes are not logged there — so I counted it on the backend side from the
uvicorn logs instead, and the number changed.

**The verification caught real bugs, including in itself.** Round robin was
broken (per-worker state). `/assets/` emitted two `Cache-Control` headers.
Unmatched routes returned Starlette's `{"detail": …}` instead of our error
envelope. And six separate measurement bugs in my own `verify.sh` — the most
embarrassing being that 120 parallel curl processes were losing most of their
output to an append race, so a check reported 31 results out of 120 and I
believed it until the numbers stopped adding up.

**I can explain every line, and where I cannot, I cut it.** That is why there is
no ORM — the locking SQL is the point of the project and an ORM would hide it
behind `with_for_update()`. It is why there is no Redis. The comments in
`holds.py` and `nginx.conf` are what I actually understand, written out, and the
places where I made a judgement call are recorded in the README's Decisions
table with the alternatives I rejected.

The honest summary: the assistant made me much faster at producing code, and
made no difference at all to whether the code was right. That part came from
running it.
</details>

---

## Part 3 — known limitations, honestly

**Security**

- `JWT_SECRET` and the Postgres password are **dev defaults committed to the
  repo**. They are clearly marked DEV-ONLY and overridden in compose, but they
  are not secrets.
- **No token revocation.** A logout clears the browser's copy; the token stays
  valid until `exp` (24 h). A deny-list would fix it.
- **The JWT lives in `localStorage`**, so an XSS bug would expose it. The strict
  same-origin CSP mitigates but does not eliminate this. The alternative —
  httpOnly cookies — brings CSRF handling and would stop Nginx caching any
  response carrying `Set-Cookie`.
- **`/nginx_status` is unauthenticated.** Deliberate for the demo; the
  `allow`/`deny` pair is in the config, commented.
- **`X-Upstream` leaks internal IPs and the replica count.** Demo only.
- **`GET /api/admin/loadtest-users` would not ship.** It is scoped to `lt%`
  fixtures and returns no hashes, but it exists purely so the load test can mint
  JWTs without tripping the login limiter.
- No account lockout, no email verification, no password reset, no MFA.
- Rate limits are **per Nginx node**. With two nodes, the effective limit
  doubles.

**Correctness and data**

- **Expired holds are never deleted.** Correct, but the tables grow. `/admin`
  shows the count.
- **No audit log.** You cannot reconstruct who held what and when.
- **`jsonb` normalises key order**, so an idempotent replay is *semantically*
  identical, not byte-identical. Fine for JSON; it would not be fine if a client
  needed to re-verify a signature over the exact bytes.
- **No payment integration.** "Pay" is a button. The idempotency machinery is
  the part that would carry over.
- **Single Postgres, no replica, no backups** in this setup.
- Seat maps are capped at 10 × 20. A stadium would need pagination or a
  different representation — 50,000 seats as one JSON blob is not viable.

**Operations**

- **Single Nginx node** — it is the single point of failure in Plan A. The three
  API replicas protect against an app crash, not against the gateway dying.
- **Self-signed certificate**, so every visitor sees a warning.
- **No metrics backend.** `/ops` reads `stub_status` and the access log; there is
  no Prometheus, no retention, no alerting.
- **Logs are not rotated** — the access log grows until the volume is pruned.
- `docker compose` is not an orchestrator: no rolling deploys, no autoscaling.
- Plan B's free tier **sleeps**, has **no shell** (so no failover demo), and has
  an ephemeral disk, so the cache and access log do not survive a restart.

**Testing**

- The "POSTs are never retried" property is a **configuration guarantee** backed
  by Nginx's documented default and the absence of the flag — demonstrated
  alongside proof that retries *are* working for GETs. It is not an end-to-end
  experiment; reliably reproducing "POST times out after the backend committed"
  needs a fault-injection hook the app does not have.
- The load test runs from **one machine and one source IP**, so per-IP limits
  dominate at high viewer counts. The tool reports that explicitly rather than
  letting it look like a cache failure.
- No frontend unit tests. The UI is verified by hand and by the browser
  walkthrough in `DEMO.md`.
- All measurements are from **one laptop**, not a controlled environment.
  Treat them as orders of magnitude, not benchmarks.
