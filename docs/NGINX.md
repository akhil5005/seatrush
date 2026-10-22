# NGINX.md — the gateway, section by section

This walks through `nginx/nginx.conf` in the order it is written, in plain
language. Every section says **what it does**, **why SeatRush needs it**, **what
breaks without it**, and gives the **exact command that proves it**.

Every piece of output below was captured from a real run on 2026-10-22 against
`docker compose up --build`. Raw transcripts are in `runs/`.

Two conventions for the commands:

- `-k` on every `curl` because the certificate is self-signed.
- Git Bash on Windows rewrites arguments that look like Unix paths, so commands
  that pass a container path (`/etc/nginx/...`, `/var/log/nginx/...`) are
  written with `MSYS_NO_PATHCONV=1` or a doubled slash (`//etc/nginx/...`).
  On macOS/Linux you can drop that.

---

## 0. What Nginx is doing here at all

Nginx is the **only** process the browser talks to. It is, in one container:

| Role | Where in the config |
|---|---|
| Static web server for the React build, with SPA fallback | `location /assets/`, `location = /index.html`, `location /` |
| Reverse proxy + round-robin load balancer | `upstream api_backend`, `proxy_pass` |
| Failover when a replica dies | `max_fails` / `fail_timeout` / `proxy_next_upstream` |
| Rate limiter (per IP, per login, per token) | `limit_req_zone` x3, `limit_conn_zone` |
| Microcache that absorbs seat-map polling | `proxy_cache_path`, `location ~ .../seats$` |
| TLS terminator | `listen 443 ssl`, `ssl_*` |
| Structured request log the ops dashboard reads | `log_format json_combined` |

**Nothing in the Python backend duplicates any of this.** There is no rate
limiter, no cache, no TLS and no load balancing in FastAPI. That separation is
the whole point: the edge handles traffic shaping, the app handles correctness.

---

## 1. Core / process model

```nginx
worker_processes auto;
error_log /dev/stderr warn;
events { worker_connections 1024; }
```

**What it does.** One **master** process reads the config, binds :80 and :443,
and owns the **workers** — one per CPU core (`auto`). A worker is
single-threaded and event-driven: it holds thousands of sockets in an `epoll`
set and is woken only for the ones with data ready. No thread per connection.

**Why SeatRush needs it.** A flash sale is mostly *idle* connections — browsers
with a seat-map page open, polling every 2.5 s. The event-driven model is what
makes 1,000 idle connections cost a few MB instead of 1,000 threads.

**What breaks without enough `worker_connections`.** Nginx logs
`worker_connections are not enough` and drops connections. You see it as a
rising `accepts - handled` gap on `/nginx_status`. Note a *proxied* request
costs **two** slots (browser↔Nginx and Nginx↔backend), so 1024 is really ~512
concurrent proxied requests per worker.

**`error_log /dev/stderr` is not cosmetic.** The official image symlinks
`/var/log/nginx/error.log → /dev/stderr`, but we mount a named volume over
`/var/log/nginx`, which destroys that symlink. Without the explicit path,
`docker compose logs nginx` would be silent.

### Proof

```bash
docker compose exec nginx sh -c 'nproc; ps -o args | grep -c "worker process"'
```

```
12
14            # 12 workers + the master + the grep itself
```

---

## 2. The per-worker trap that nearly broke the load balancer

```nginx
upstream api_backend {
    zone api_backend 64k;
    server api1:8000 max_fails=2 fail_timeout=10s;
    ...
}
```

**This is the single most useful thing I learned building the project.**

Round-robin position and the `max_fails` failure counters live in **each
worker's private memory** by default. With `worker_processes auto` on a 12-core
machine that is **12 independent counters, each starting at api1**. Twelve
separate browser connections therefore all go to api1 and the load balancing
looks completely broken.

I measured exactly that:

```bash
# before `zone`
for i in $(seq 1 9); do
  curl -sk -D - -o /dev/null https://localhost:8443/api/health | grep -i '^x-instance'
done | sort | uniq -c
```

```
      9 x-instance: api1          # <-- all nine to one replica
```

But on a **single reused keepalive connection** (one worker), the very same
config cycled correctly:

```
x-instance: api3
x-instance: api1
x-instance: api2
```

That is the tell: per-connection distribution broken, per-worker distribution
fine ⇒ the state is per worker. `zone api_backend 64k;` moves it into shared
memory. The second half matters just as much: without it, worker 7 marking api2
as down teaches the other 11 workers nothing.

### Proof (after the fix)

```bash
for i in $(seq 1 30); do
  curl -sk -D - -o /dev/null https://localhost:8443/api/health | grep -i '^x-instance'
done | sort | uniq -c
```

```
     10 x-instance: api1
     10 x-instance: api2
     10 x-instance: api3
```

**Why round robin is the right algorithm here.** The replicas are *stateless*:
auth is a self-contained JWT verified with a shared secret, and every piece of
mutable state (seats, holds, bookings, idempotency keys) lives in Postgres. Any
replica can serve any request from any user. So **no sticky sessions are
needed** — which is exactly why `ip_hash` would be a downgrade: it would pin a
whole NATed office to one replica for no benefit.

`least_conn` and `ip_hash` are in the config, commented, with the one-line
reason each would be the right choice.

---

## 3. Upstream keepalive — two lines that are easy to miss

```nginx
# upstream block
keepalive 32;
keepalive_requests 1000;
keepalive_timeout 60s;

# snippets/proxy-headers.conf
proxy_http_version 1.1;
proxy_set_header Connection "";
```

**What it does.** Keeps up to 32 **idle** connections to the backends cached
per worker, so a proxied request usually costs zero TCP handshakes.

**The trap.** By default Nginx talks to upstreams with **HTTP/1.0** and sends
`Connection: close`. So without `proxy_http_version 1.1` *and* the emptied
`Connection` header, the `keepalive 32` pool is **silently never used**.
Nothing errors. You just quietly pay a TCP handshake per request and pile up
sockets in `TIME_WAIT`.

**Why SeatRush needs it.** At ~400 seat-map polls/second, a handshake per
request is 400 extra connection setups/second and eventual ephemeral-port
exhaustion.

### Proof

```bash
MSYS_NO_PATHCONV=1 docker compose exec nginx grep -nE 'proxy_http_version|Connection' //etc/nginx/snippets/proxy-headers.conf
```

---

## 4. Passive health checks and failover

```nginx
server api2:8000 max_fails=2 fail_timeout=10s;

proxy_next_upstream         error timeout http_502 http_503 http_504;
proxy_next_upstream_tries   3;
proxy_next_upstream_timeout 5s;
```

**Open-source Nginx has only PASSIVE health checks.** There is no prober —
Nginx never calls `/api/health` on its own. The `health_check` directive is
**Nginx Plus**. Instead Nginx judges a server by how *real traffic* to it
behaves: after `max_fails=2` failures within `fail_timeout=10s`, the server is
marked unavailable for 10 s, then one request is let through to see if it
recovered.

**The honest consequence:** the first request after a replica dies is the one
that *discovers* it. That request is not lost — `proxy_next_upstream` retries
it elsewhere — but somebody pays the `proxy_connect_timeout`. This is also why
the app ships its own **active** prober at `GET /api/ops/cluster`.

### Proof — kill a replica and watch a request survive

```bash
docker compose kill api2        # SIGKILL, like a crash
for i in $(seq 1 12); do
  curl -sk -D - -o /dev/null https://localhost:8443/api/health | grep -i '^x-upstream'
done | sort | uniq -c
```

```
      5 X-Upstream: 172.21.0.3:8000
      1 X-Upstream: 172.21.0.4:8000, 172.21.0.3:8000     <-- a retry
      1 X-Upstream: 172.21.0.4:8000, 172.21.0.5:8000     <-- a retry
      5 X-Upstream: 172.21.0.5:8000
```

All twelve returned **200**. Two of them show *two* upstream addresses: Nginx
tried the dead api2 (`.0.4`), failed, and retried on a healthy replica. After
`max_fails=2`, api2 was taken out of rotation and the remaining ten went
straight to a live replica.

The same event in the access log:

```json
{
  "request_uri": "/api/health",
  "status": 200,
  "request_time": 3.005,
  "upstream_addr": "172.21.0.4:8000, 172.21.0.5:8000",
  "upstream_status": "504, 200",
  "upstream_response_time": "3.003, 0.002"
}
```

`upstream_status: "504, 200"` — first attempt timed out after the 3 s
`proxy_connect_timeout`, second attempt succeeded. The client saw one 200.

### Proof — it rejoins

```bash
docker compose start api2 && sleep 12      # > fail_timeout
for i in $(seq 1 30); do
  curl -sk -D - -o /dev/null https://localhost:8443/api/health | grep -i '^x-instance'
done | sort | uniq -c
```

```
     10 x-instance: api1
     10 x-instance: api2
     10 x-instance: api3
```

---

## 5. Why POSTs are never retried

Two things are **deliberately absent** from `proxy_next_upstream`:

**`http_500` is absent.** A 500 means the request *reached* the application and
the application blew up — possibly *after* committing a booking. Replaying it
on another replica could produce a second booking. An honest 500 to one user
beats a silent double-book.

**`non_idempotent` is absent — this is the important one.** By default Nginx
will **not** retry a POST/PATCH/LOCK, even on a timeout; it only retries
requests it considers safe to replay. Adding `non_idempotent` to the list would
tell it to retry POSTs too, and a timed-out-but-actually-succeeded
`POST /api/holds/{id}/confirm` would then be sent again: **two bookings, one
payment.**

So the division of labour is:

- **Nginx's job:** never *manufacture* a duplicate booking.
- **The app's job** (`backend/app/idempotency.py`): absorb the duplicates Nginx
  cannot prevent — the user's own double-click, the browser's own retry.

### Proof

```bash
grep -n 'non_idempotent' nginx/nginx.conf
```

```
372:    #   * `non_idempotent` is absent, which is the important one. By default
375:    #     `non_idempotent` to this list would tell it to retry POSTs too, and
685:        # non_idempotent, so a POST that times out here is reported to the user
```

Three hits, **all of them comments** — the directive is never set. Section 4
above proves the retry machinery *is* active for GETs (`"504, 200"`), so the
only thing standing between a POST and a replay is this rule.

> **Honest scope note.** This is a *configuration* guarantee backed by Nginx's
> documented default, demonstrated by the absence of the flag plus the proof
> that retries are otherwise working. It is not an end-to-end experiment —
> reliably reproducing "POST times out after the backend committed" needs a
> fault-injection hook the app does not have.

---

## 6. Timeouts and forwarded headers

`snippets/proxy-headers.conf`:

```nginx
proxy_connect_timeout 3s;
proxy_send_timeout    10s;
proxy_read_timeout    10s;

proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

**Timeouts.** Nginx's defaults are 60 s read/send: one stuck backend would tie
up a worker connection for a full minute *per request*, and a flash sale would
exhaust `worker_connections` while users stared at spinners.

- `connect 3s` — on a local network a backend either answers in milliseconds or
  it is down. A fast failure is what lets `proxy_next_upstream` move on
  quickly. This is the 3 s visible in `request_time: 3.005` in section 4.
- `read 10s` — the important one: time between two successive reads from the
  backend. Protects against a backend that accepted the connection and then
  hung (e.g. blocked on a database lock). Comfortably above our slowest real
  query, well below a user's patience.

**Forwarded headers.** The backend sees Nginx's container IP as `$remote_addr`,
so without these the app and its logs would think every request came from the
proxy. `X-Forwarded-Proto` is how the app learns the user's connection was
HTTPS, because **TLS stops at Nginx** and the backends speak plain HTTP on the
private Docker network.

`Host $host` (not `$proxy_host`) passes the browser's Host through unchanged;
`$proxy_host` would rewrite it to `api_backend`, which is wrong.

**One demo-only header:**

```nginx
add_header X-Upstream $upstream_addr always;   # REMOVE IN PRODUCTION
```

It is what makes load balancing and failover *visible* (see section 4). In
production it leaks internal topology — private IPs, ports, replica count —
straight to anyone with a browser.

### Proof — request body buffering is left ON, on purpose

```nginx
# proxy_request_buffering off;   # <- do NOT enable for this app
```

With buffering on (the default), Nginx reads the whole body from the client
*before* opening a connection to a backend, so a slow client occupies a cheap
Nginx slot instead of pinning a uvicorn worker. Turning it off would hand
slowloris a path straight to the Python processes and undo
`client_body_timeout`'s whole purpose.

---

## 7. Rate limiting — the leaky bucket

```nginx
limit_req_zone $binary_remote_addr  zone=api_ip:10m        rate=20r/s;
limit_req_zone $binary_remote_addr  zone=auth_ip:10m       rate=5r/m;
limit_req_zone $http_authorization  zone=booking_user:10m  rate=2r/s;
limit_conn_zone $binary_remote_addr zone=conn_ip:10m;

limit_req_status  429;
limit_conn_status 429;
```

**The mental model.** Each key (an IP, a token) gets a bucket that drains at a
constant `rate`. Every request adds one drop.

| Knob | Meaning |
|---|---|
| `rate` | the sustained speed allowed, long term |
| `burst` | how many extra drops may sit in the bucket at once — a small waiting room for a spike |
| `nodelay` | serve those queued drops **immediately** instead of spacing them out at `rate` |

**Why `nodelay`.** Without it, a burst of 40 at 20 r/s means the 40th request
*waits ~2 s* before being proxied — a user staring at a frozen page. With it,
all 40 go through at once and the bucket then refuses new requests until it has
drained. For an interactive flash sale that is the right trade:
**fast-and-sometimes-429 beats uniformly-slow.**

**Why 429 and not the default 503.** 503 means "the server is broken"; 429 means
"you are going too fast, and here is how long to wait". A client library retries
a 429 sensibly and gives up on a 503.

`$binary_remote_addr` (4 bytes for IPv4) rather than `$remote_addr` (up to 15
bytes of text) purely to fit more keys in the same shared memory. 10 MB holds
roughly 160k keys.

### 7a. The general API ceiling — 20 r/s, burst 40

```bash
for i in $(seq 1 120); do
  curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8443/api/time &
done | sort | uniq -c; wait
```

```
     78 200
     42 429
```

### 7b. The 429 response in full

```bash
for i in $(seq 1 100); do curl -sk -D /tmp/h_$i -o /tmp/b_$i https://localhost:8443/api/time & done; wait
# then print the headers+body of any that was rejected
```

```
HTTP/1.1 429 Too Many Requests
Server: nginx
Content-Type: application/json
Retry-After: 1
Cache-Control: no-store
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: geolocation=(), camera=(), microphone=(), payment=()
Content-Security-Policy: default-src 'self'; script-src 'self'; ...
Strict-Transport-Security: max-age=300

{"error":{"code":"rate_limited","message":"Too many requests, please slow down","retry_after":1}}
```

Three things to notice:

1. **A JSON body in our envelope**, via `error_page 429 = @rate_limited;`.
   Without it the edge returns an HTML page, the frontend's `response.json()`
   throws, and the user sees "something went wrong" instead of "too many
   requests, slow down".
2. **`Retry-After: 1`** — the machine-readable "come back in N seconds". The
   frontend reads it to time its toast instead of guessing.
3. **Every security header is present on an error response**, because of
   `always` plus re-including the snippet. See section 10.

### 7c. Login is ~1000× stricter — 5 r/**m**, burst 3

```bash
for i in $(seq 1 8); do
  curl -sk -o /dev/null -w '%{http_code}\n' -X POST https://localhost:8443/api/auth/login \
    -H 'Content-Type: application/json' -d '{"email":"nobody@x.io","password":"wrongpass"}'
done
```

```
401 401 401 401 429 429 429 429
```

Four attempts get through (burst 3 + the one the bucket allows), then the edge
takes over. Note `r/m`, not `r/s` — a password guess is cheap for the attacker
and expensive for us: **every attempt costs a ~50 ms scrypt hash.** This limit
protects CPU as much as it protects accounts.

### 7d. Per-**token** booking limit — the interesting one

```nginx
location /api/holds {
    limit_req zone=booking_user burst=5 nodelay;
    limit_req zone=api_ip       burst=40 nodelay;
    limit_conn conn_ip 50;
}
```

The key is `$http_authorization`, so **each JWT gets its own bucket**.

Why per-token and not per-IP: a per-IP limit is useless when 400 students share
one college NAT, and too loose when one user scripts 50 hold attempts from one
laptop. Per-token gets both cases right.

```bash
# token A: 12 rapid authenticated requests
for i in $(seq 1 12); do curl -sk -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $TOK_A" https://localhost:8443/api/holds/<uuid> & done | sort | uniq -c; wait

# token B, SAME IP, same instant
for i in $(seq 1 5); do curl -sk -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $TOK_B" https://localhost:8443/api/holds/<uuid> & done | sort | uniq -c; wait
```

```
token A:     6 404        # 404 = the uuid does not exist; the request got THROUGH
             6 429        # rate limited

token B:     5 404        # all five through -- its own bucket, same IP
```

That is the proof in one shot: token A is throttled while token B, from the
**same IP at the same instant**, is untouched.

**Two subtleties:**

1. **Both zones are listed.** `limit_req` is *not* additive across levels — a
   location that declares any `limit_req` inherits **none** from the parent.
   Declaring both means a request must satisfy both buckets.
2. **An empty key is not counted at all.** A request with no `Authorization`
   header produces an empty key and Nginx skips the zone entirely. So anonymous
   traffic slips past `booking_user` — which is exactly why the location also
   applies `api_ip`, and why the backend rejects an unauthenticated
   `POST /api/holds` with 401 regardless.
3. The key is a few hundred bytes of JWT, so this zone holds ~25k distinct
   tokens rather than 160k. A production system would key on a short user id
   extracted at the edge (the `auth_request` stretch goal).

### 7e. Connection limiting

```nginx
limit_conn conn_ip 50;
```

Concurrent, not per-second. Stops one client from opening hundreds of sockets
and sitting on them — each costs a `worker_connections` slot whether or not it
is sending anything.

---

## 8. Microcaching — the 400× win

```nginx
proxy_cache_path /var/cache/nginx/api levels=1:2 keys_zone=api_cache:10m
                 max_size=100m inactive=10m use_temp_path=off;

location ~ ^/api/events/[0-9]+/seats$ {
    proxy_cache       api_cache;
    proxy_cache_valid 200 1s;
    proxy_cache_methods GET HEAD;
    proxy_cache_lock on;
    proxy_cache_lock_timeout 2s;
    proxy_cache_use_stale updating error timeout http_502 http_503;
    proxy_cache_background_update on;
    add_header X-Cache-Status $upstream_cache_status always;
}
```

### The problem

Every open seat-map tab polls `GET /api/events/{id}/seats` every ~2.5 s.
**1,000 people watching one event ≈ 400 requests/second**, all asking the
identical question, all hitting Postgres. Three uvicorn replicas would spend
their entire capacity answering a question whose answer barely changes.

### The fix

Cache that answer for **one second**. 400 req/s collapses to ~1 req/s per event
reaching a backend — and users still see a map at most 1 s old.

### Why 1 s of staleness is safe *here*

Because the seat map is only ever a **hint**. Seat ownership is decided
exclusively by the conditional `UPDATE` in Postgres
(`backend/app/holds.py:claim_seats`). The worst case is: a user sees a seat as
free, clicks it, and gets a clean **409 `seat_taken`** with the conflicting seat
highlighted. That is a normal, handled outcome — not a correctness failure.

We traded one second of visual freshness for a 400× capacity win, and the
invariant "never two owners for one seat" was never involved.

### `proxy_cache_lock` — the thundering-herd fix

Without it, the instant a 1-second entry expires, **all 400 waiting pollers are
simultaneously declared cache misses and all 400 are forwarded to the
backends** — a stampede every single second, which is worse than no cache at
all. With it, **one** request is allowed through to refill the entry and the
rest wait for it.

`proxy_cache_use_stale updating` + `proxy_cache_background_update on` go
further: serve the slightly stale copy *immediately* and refresh in the
background, so no user ever waits for a refill. `error timeout http_502
http_503` in that list is what keeps the seat map rendering during the
`docker stop api2` demo.

### Why these endpoints and no others

The default cache key is `$scheme$proxy_host$request_uri` — it does **not**
include the `Authorization` header. So a cached response is served to whoever
asks for that URL next. That is only safe for responses that do not depend on
the caller.

`/api/events*` qualifies **by design**: no auth is read, and the seat map never
says *who* holds a seat. The backend has a test that enforces this
(`test_cacheable_endpoints_ignore_the_caller`) — if someone makes the seat map
user-specific, the test fails before the cache can leak anything.

Nginx also refuses to cache any response carrying `Set-Cookie`, a useful second
safety net — but we do not rely on it, because this API is JWT-based and sets no
cookies at all (`test_cacheable_endpoints_set_no_cookies`).

### Proof — MISS then HIT, and what is never cached

```bash
cache_of() { curl -sk -D - -o /dev/null -H "Authorization: Bearer $TOK" "https://localhost:8443$1" \
             | grep -i '^x-cache-status' | tr -d '\r' | cut -d' ' -f2; }
```

| Path | 1st request | 2nd request |
|---|---|---|
| `/api/events` | `MISS` / `STALE` | **`HIT`** |
| `/api/events/1` | `MISS` | **`HIT`** |
| `/api/events/1/seats` | `MISS` / `STALE` | **`HIT`** |
| `/api/time` | *(no header — never cached)* | *(no header)* |
| `/api/bookings` | *(no header — never cached)* | *(no header)* |
| `/api/auth/me` | *(no header — never cached)* | *(no header)* |
| `/api/ops/cluster` | *(no header — never cached)* | *(no header)* |
| `/api/admin/events/1/stats` | *(no header — never cached)* | *(no header)* |
| `/api/holds/<uuid>` | *(no header — never cached)* | *(no header)* |

The absence of the header is the proof: `X-Cache-Status` is only added inside
the three cached locations, and `proxy_cache` is set **nowhere else** — not at
`http` or `server` level — so there is nothing for the other locations to
inherit and nothing to bypass.

Caching a login response would be the single worst bug possible here: **one
user's JWT handed to the next caller.**

### Proof — the poll storm collapses

```bash
for i in $(seq 1 60); do
  curl -sk -D - -o /dev/null https://localhost:8443/api/events/1/seats | grep -i '^x-cache-status' &
done | sort | uniq -c; wait
```

```
     58 X-Cache-Status: HIT
      2 X-Cache-Status: STALE
```

**Zero `MISS`** — 60 concurrent pollers, no backend request. And a cached hit
shows up in the access log with an *empty* `upstream_addr`, which is the
clearest possible evidence that no backend was contacted:

```json
{
  "request_uri": "/api/events/1/seats",
  "status": 200,
  "upstream_addr": "",
  "upstream_status": "",
  "upstream_cache_status": "STALE",
  "body_bytes_sent": 6593
}
```

`loadtest/flashsale.py --poll` measures the HIT ratio properly over 20 s with
200 simulated viewers.

### Why the event list gets 2 s and the seat map 1 s

`seats_left` is the only thing that moves on the list page, and the list is not
a live dashboard. The seat map is what a user stares at while deciding, so it
gets the tightest window that still collapses a storm.

---

## 9. Request size and slow-client hygiene

```nginx
client_max_body_size  64k;
client_header_timeout 10s;
client_body_timeout   10s;
client_header_buffer_size   4k;
large_client_header_buffers 4 16k;
```

**`client_max_body_size 64k`.** This app has **no uploads**. The biggest
legitimate request body is a hold with 6 seat ids — about 80 bytes. Anything
larger is rejected with **413 at the edge**: the bytes never reach a Python
process and never occupy a uvicorn worker.

**The timeouts are slowloris protection.** A client that opens a connection,
sends one byte of header per second and never finishes would otherwise hold a
worker-connection slot indefinitely; a few thousand of those exhaust
`worker_connections` and the site is down *without any bandwidth being used*.

**`large_client_header_buffers`** is raised because a JWT in an `Authorization`
header is long and the default 8k gets tight.

### Proof

```bash
python -c "import json;print(json.dumps({'event_id':1,'seat_ids':[1]*40000}))" > /tmp/big.json
ls -l /tmp/big.json                      # 120031 bytes
curl -sk -X POST https://localhost:8443/api/holds \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: k' \
  --data-binary @/tmp/big.json
```

```
HTTP/1.1 413 Request Entity Too Large
Content-Type: application/json

{"error":{"code":"payload_too_large","message":"Request body exceeds the 64 KB limit"}}
```

A 120 KB body, rejected at the edge, in our own JSON envelope.

---

## 10. Compression

```nginx
gzip on;
gzip_comp_level 5;
gzip_min_length 256;
gzip_proxied any;
gzip_types application/json application/javascript text/javascript
           text/css text/plain text/xml application/xml
           image/svg+xml application/manifest+json;
gzip_vary on;
```

**Why it matters for this app specifically.** The seat map for a 10×20 venue is
very repetitive JSON and it is re-fetched every 2.5 s by every open tab.

### Proof

```bash
curl -sk -o /dev/null -w '%{size_download}\n' https://localhost:8443/api/events/3/seats
curl -sk -H 'Accept-Encoding: gzip' -o /dev/null -w '%{size_download}\n' https://localhost:8443/api/events/3/seats
```

```
14051      # uncompressed
 1601      # gzipped  -> 8.8x smaller
```

On a flash sale with 1,000 viewers that is the difference between ~5 MB/s and
~0.6 MB/s of egress.

- `gzip_comp_level 5` is the knee of the curve: nearly level-9 ratio at a
  fraction of the CPU.
- `gzip_min_length 256` — below that the gzip header costs more than it saves.
- `gzip_proxied any` because our backends send no `Cache-Control`/`Expires`
  hints; without it, proxied responses would not be compressed at all.
- `gzip_vary on` adds `Vary: Accept-Encoding`, so a shared cache (ours, or a CDN
  later) never serves a gzipped body to a client that cannot decompress it.
- `text/html` is always compressed and must **not** be listed again.

---

## 11. Security headers and the `add_header` inheritance trap

`snippets/security-headers.conf` is included by **every single location**, even
ones that look like they would inherit it. That is not belt-and-braces; it is
required.

### *** THE TRAP ***

`add_header` is **not additive across configuration levels**:

- a location with **no** `add_header` of its own inherits the whole set from its
  `server`/`http` block;
- a location that defines **even one** `add_header` inherits **none** of them.

So the moment a location adds `X-Cache-Status`, it **silently loses every
security header** the server block was adding. They do not merge — the nearest
level with any `add_header` wins outright.

In this config four locations add their own headers (`/assets/` adds
`Cache-Control`, `= /index.html` adds `Cache-Control`, and the three cached API
locations add `X-Cache-Status`). Every one of them would have been stripped bare
without the explicit include.

### `always` matters too

`always` means "send this header even on error responses" (4xx/5xx). Without it,
a 429 or a 502 would come back with **no security headers at all** — and error
pages are exactly the responses an attacker is most likely to see.

### Proof — count the headers per location

```bash
check() {
  n=$(curl -sk -D - -o /dev/null "https://localhost:8443$1" | grep -icE \
    '^(x-content-type-options|x-frame-options|referrer-policy|permissions-policy|content-security-policy|strict-transport-security)')
  own=$(curl -sk -D - -o /dev/null "https://localhost:8443$1" | grep -icE '^(x-cache-status|cache-control)')
  printf "  %-30s security-headers=%s  own-headers=%s\n" "$1" "$n" "$own"
}
```

```
  /api/events/1/seats            security-headers=6  own-headers=1
  /api/events                    security-headers=6  own-headers=1
  /assets/index-DTupW3es.js      security-headers=6  own-headers=1
  /index.html                    security-headers=6  own-headers=1
  /nginx_status                  security-headers=6  own-headers=0
  /api/health                    security-headers=6  own-headers=0
```

Six out of six everywhere, **including the four locations that define their
own** headers. If one of those includes were removed, its row would read
`security-headers=0`.

### The headers themselves

| Header | Why |
|---|---|
| `X-Content-Type-Options: nosniff` | stops the browser guessing a JSON response is HTML and executing it |
| `X-Frame-Options: DENY` | clickjacking — nobody may iframe the seat map and trick a logged-in user into clicking "Pay". `DENY` is stricter than `SAMEORIGIN` and we never self-frame |
| `Referrer-Policy: strict-origin-when-cross-origin` | stops a hold id or booking code in a URL leaking to a third party via `Referer` |
| `Permissions-Policy` | browsers have no business asking for camera/mic/geolocation here |
| `Content-Security-Policy` | same-origin only — see below |
| `Strict-Transport-Security` | HTTPS only — see below |

**CSP.** `default-src 'self'; script-src 'self'; style-src 'self'; img-src
'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self';
form-action 'self'`.

A strict same-origin policy is *possible* precisely because this app has no CDN,
no external fonts and no third-party analytics: Nginx serves the React bundle
and proxies `/api` from one origin.

- `script-src 'self'` means **no inline `<script>` and no `eval`**. A Vite
  *production* build emits an external module script, so this works. `npm run
  dev` would need `'unsafe-inline'` for its HMR client — which is why dev is
  proxied *through Vite*, not through Nginx.
- `style-src 'self'` does **not** break React's `style={{...}}` props: React
  writes those through the CSSOM (`element.style`), which CSP does not police.
  CSP only blocks literal `style=""` attributes in markup.
- `connect-src 'self'` means an injected script cannot exfiltrate a JWT to
  another host.

**HSTS, and why it is sticky and risky on localhost.** HSTS tells the browser
"never speak plain HTTP to this host again", and the browser **remembers** it.
A long `max-age` on `localhost` would pin *every future project you run on
localhost* to HTTPS, including ones with no certificate, and the only cure is
digging into `chrome://net-internals/#hsts`.

So `max-age` is deliberately **300 seconds** here (production would be a year
with `includeSubDomains` and `preload`), and it is emitted **only over HTTPS**
via a neat trick:

```nginx
map $scheme $hsts_header {
    default "";
    https   "max-age=300";
}
# in the snippet:
add_header Strict-Transport-Security $hsts_header always;
```

Nginx **skips an `add_header` whose variable value is an empty string**, so one
shared snippet can be included in the plain-HTTP server block too without
leaking HSTS over HTTP. One file, correct in both servers.

---

## 12. TLS termination and the redirect

```nginx
# server 1 of 2 -- plain HTTP, redirect only
listen 80 default_server;
return 301 https://$host:8443$request_uri;
```

```nginx
# server 2 of 2 -- HTTPS
listen 443 ssl default_server;
http2 on;
ssl_certificate     /etc/nginx/certs/server.crt;
ssl_certificate_key /etc/nginx/certs/server.key;
ssl_protocols       TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_session_cache   shared:SSL:10m;
ssl_session_timeout 1h;
ssl_session_tickets off;
```

**The port in the redirect is the whole trick.** In production you would write
`https://$host$request_uri` and let `:443` be implicit. Here Nginx listens on
container `:443` but it is published on **host `:8443`**, and `$host` does *not*
include a port. Without the explicit `:8443` the browser would be sent to
`https://localhost` (port 443), where nothing is listening, and the demo dies on
step one.

301 (permanent) rather than 302 so browsers cache it — which is also why the
line has to be right *before* anyone loads the page.

**`http2 on;` as its own directive** is the modern form (Nginx ≥ 1.25.1). The old
`listen 443 ssl http2;` still parses but is deprecated — one of the reasons the
image tag is pinned to `nginx:1.27-alpine`. HTTP/2 matters here because the
seat-map page issues its poll alongside other requests, and HTTP/2 multiplexes
them over **one** TCP+TLS connection instead of opening up to 6 per origin.

**TLS ends at Nginx.** Everything behind it — api1/api2/api3, Postgres — speaks
plain HTTP/TCP on the private Docker network. That is why the backends are cheap
(no per-replica certificate, no TLS CPU cost) and why they must learn the
original scheme from `X-Forwarded-Proto`. In a zero-trust production network you
would re-encrypt this hop.

**`ssl_prefer_server_ciphers off`** lets the *client* choose. Modern clients pick
the fastest suite their hardware supports (AES-NI vs ChaCha20); forcing the
server's order is 2015 advice that now makes mobile slower.

**The certificate needs no manual step.** `scripts/gen-cert.sh` is copied into
the image as `/docker-entrypoint.d/10-gen-cert.sh`; the official entrypoint runs
every executable script there before starting Nginx. It is a no-op if a cert
already exists, so restarts reuse the same key (otherwise the browser would warn
anew every time).

### Proof

```bash
curl -s -i http://localhost:8080/events/1 | head -8
```

```
HTTP/1.1 301 Moved Permanently
Server: nginx
Location: https://localhost:8443/events/1
X-Content-Type-Options: nosniff
```

```bash
echo | openssl s_client -alpn h2,http/1.1 -connect localhost:8443 -servername localhost 2>&1 \
  | strings | grep -iE "ALPN protocol|Protocol  *:|Cipher"
```

```
New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384
ALPN protocol: h2
    Protocol  : TLSv1.3
```

```bash
echo | openssl s_client -connect localhost:8443 -servername localhost 2>/dev/null \
  | openssl x509 -noout -subject -dates -ext subjectAltName
```

```
subject=C=IN, ST=Karnataka, L=Bengaluru, O=SeatRush Dev, CN=localhost
notBefore=Oct 22 06:12:43 2026 GMT
notAfter=Oct 22 06:12:43 2027 GMT
X509v3 Subject Alternative Name:
    DNS:localhost, DNS:seatrush.local, IP Address:127.0.0.1
```

`subjectAltName` is not optional: modern browsers ignore `CN` entirely and look
only at the SAN, so a cert without it is rejected outright.

> **Note on verifying HTTP/2 from Git Bash.** The bundled `curl` on this machine
> has no HTTP/2 support (`curl: option --http2: the installed libcurl version
> does not support this`), which is why the proof above uses `openssl
> s_client`. Cross-checked with an HTTP/2-capable curl in a container:
> `docker run --rm --network seatrush_seatrush curlimages/curl:8.11.1 -sk
> --http2 -o /dev/null -w '%{http_version}' https://nginx/api/health` → `2`.

---

## 13. Static frontend and the SPA fallback

```nginx
root /usr/share/nginx/html;

location /assets/ {
    add_header Cache-Control "public, max-age=31536000, immutable" always;
    include /etc/nginx/snippets/security-headers.conf;
    access_log off;
    try_files $uri =404;
}

location = /index.html {
    add_header Cache-Control "no-cache" always;
    include /etc/nginx/snippets/security-headers.conf;
}

location / {
    include /etc/nginx/snippets/security-headers.conf;
    try_files $uri $uri/ /index.html;
}
```

**Hashed assets get a year.** Vite emits `/assets/index-DTupW3es.js`, so the
filename changes whenever the content changes. That is what makes a one-year
cache safe: there is nothing to invalidate, only new names. `immutable`
additionally tells the browser not to revalidate even on a hard refresh.

### A bug I found and fixed here

The obvious way to write that line is `expires 1y;` **plus**
`add_header Cache-Control "public, immutable";` — and it is wrong. `expires`
emits its **own** `Cache-Control`, so the response went out with **two**:

```
Cache-Control: max-age=31536000
Cache-Control: public, immutable
```

Which one a cache honours is then up to the cache, and `immutable` may be
dropped entirely. Writing the single complete value by hand removes the
ambiguity:

```bash
curl -sk -D - -o /dev/null https://localhost:8443/assets/index-DTupW3es.js | grep -icE '^cache-control'
```

```
1
Cache-Control: public, max-age=31536000, immutable
```

**`index.html` must never be cached.** Otherwise a user with the page open keeps
loading the *old* `/assets/index-<oldhash>.js` forever after a deploy — a 404 or
a stale app with no way to recover but a hard refresh. `no-cache` means
"revalidate every time", **not** "do not store": the browser still gets a cheap
304 when nothing changed.

**`access_log off` for `/assets/`.** Asset hits are ~90% of the request count
and 0% of the interesting information. Turning them off keeps the ops
dashboard's live table focused on API traffic.

**The SPA fallback.** React Router owns `/events/12` and `/checkout/<uuid>`, but
those paths do not exist as files, so a hard refresh or a shared link would 404
without `try_files $uri $uri/ /index.html`. This block uses the plain `/`
prefix, the **lowest-priority** match in Nginx, so every `/api/` and `/assets/`
rule wins over it.

### Proof

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8443/checkout/abc-123
```

```
200          # served index.html; React Router reads the URL
```

---

## 14. Location precedence — why the ordering works

Nginx does **not** match locations top-to-bottom. The real algorithm:

1. `location = /exact` wins immediately.
2. The **longest** `location /prefix` match is remembered.
3. Every `location ~ regex` is tried **in file order**; the first hit wins and
   the remembered prefix is discarded.
4. If no regex matched, the remembered prefix is used.

So in this config:

| Request | Matched by | Why |
|---|---|---|
| `/api/events` | `location = /api/events` | exact match, rule 1 |
| `/api/events/1` | `location ~ ^/api/events/[0-9]+$` | regex beats the `/api/` prefix |
| `/api/events/1/seats` | `location ~ ^/api/events/[0-9]+/seats$` | listed before the other regex |
| `/api/auth/login` | `location = /api/auth/login` | exact |
| `/api/holds/x/confirm` | `location /api/holds` | no regex matches; longest prefix |
| `/api/bookings` | `location /api/` | the general fallback |
| `/nginx_status` | `location = /nginx_status` | exact |
| `/checkout/abc` | `location /` | lowest-priority prefix |

`/api/holds` is a **prefix** match on purpose: it covers `/api/holds`,
`/api/holds/{id}`, `/api/holds/{id}/confirm` and the `DELETE` in one block, so
the per-user rate limit cannot be forgotten on one of them.

---

## 15. Logging and metrics

```nginx
log_format json_combined escape=json
    '{"time":"$time_iso8601","remote_addr":"$remote_addr",'
    '"request_method":"$request_method","request_uri":"$request_uri",'
    '"status":$status,"request_time":$request_time,'
    '"upstream_addr":"$upstream_addr","upstream_status":"$upstream_status",'
    '"upstream_response_time":"$upstream_response_time",'
    '"upstream_cache_status":"$upstream_cache_status",'
    '"body_bytes_sent":$body_bytes_sent,"http_user_agent":"$http_user_agent"}';

access_log /var/log/nginx/access.json.log json_combined buffer=32k flush=1s;
```

**`escape=json` is mandatory, not cosmetic.** It escapes quotes, backslashes and
control characters inside variables. Without it, a `User-Agent` containing a
double quote produces a line that is **not valid JSON**, and the ops dashboard's
`json.loads()` silently drops that request from the view.

**The `upstream_*` fields are deliberately quoted as STRINGS.** After a retry
Nginx writes a comma-separated list — `upstream_addr` can be
`"172.21.0.4:8000, 172.21.0.5:8000"` and `upstream_status` `"504, 200"` (see
section 4). Declaring them as numbers is a bug that only appears during a
failover — i.e. exactly when you are reading the log.

**`buffer=32k flush=1s`** batches writes so a flash sale does not become one
`write(2)` syscall per request, while bounding how stale the ops dashboard can
be.

**One writer, many readers.** The log lives on a named volume that Nginx mounts
**read-write** and the API containers mount **read-only** (`nginx_logs:/var/log/nginx:ro`
in `docker-compose.yml`), so `GET /api/ops/logs` can tail it with no locking and
no chance of an API process corrupting it. `backend/app/ops.py` seeks backwards
from the end in 8 KB blocks, so the cost does not grow with log size.

### Proof

```bash
MSYS_NO_PATHCONV=1 docker compose exec nginx tail -1 //var/log/nginx/access.json.log \
  | python -c "import sys,json;print(json.dumps(json.loads(sys.stdin.read()),indent=2))"
```

```json
{
  "time": "2026-10-22T06:24:21+00:00",
  "remote_addr": "172.21.0.1",
  "request_method": "GET",
  "request_uri": "/api/health",
  "status": 200,
  "request_time": 0.002,
  "upstream_addr": "172.21.0.3:8000",
  "upstream_status": "200",
  "upstream_response_time": "0.002",
  "upstream_cache_status": "",
  "body_bytes_sent": 49,
  "http_user_agent": "curl/8.16.0"
}
```

The `python -c json.loads` round-trip *is* the test: if `escape=json` were
missing, it would throw.

### `/nginx_status`

```nginx
location = /nginx_status {
    stub_status;
    access_log off;
    include /etc/nginx/snippets/security-headers.conf;
}
```

```bash
curl -sk https://localhost:8443/nginx_status
```

```
Active connections: 1
server accepts handled requests
 496 496 496
Reading: 0 Writing: 1 Waiting: 0
```

**`accepts` minus `handled` is the number of connections Nginx DROPPED** because
it hit a resource limit — the first number to look at when the site feels slow.
Here `496 - 496 = 0`. `backend/app/ops.py:parse_stub_status` computes it and the
ops dashboard shows it.

`access_log off` because self-monitoring must not pollute the log it is
monitoring.

**PRODUCTION: this must be restricted.** It is an unauthenticated window into
your traffic volume. The config carries the commented `allow`/`deny` pair; it is
left open here only so the ops dashboard can read it during a screen-shared
demo.

---

## 16. `server_tokens off` and one error envelope

```bash
curl -sk -D - -o /dev/null https://localhost:8443/ | grep -i '^server:'
```

```
Server: nginx          # no version -- free recon denied
```

Every error, whether it comes from Nginx or from Python, uses **one shape**:

```bash
curl -sk https://localhost:8443/api/nope                     # 404
curl -sk -X DELETE https://localhost:8443/api/events         # 405
curl -sk https://localhost:8443/api/events/999999            # 404
```

```json
{"error":{"code":"not_found","message":"Not Found"}}
{"error":{"code":"method_not_allowed","message":"Method Not Allowed"}}
{"error":{"code":"event_not_found","message":"No such event"}}
```

Edge errors match:

```json
{"error":{"code":"rate_limited","message":"Too many requests, please slow down","retry_after":1}}
{"error":{"code":"payload_too_large","message":"Request body exceeds the 64 KB limit"}}
{"error":{"code":"upstream_unavailable","message":"The booking service is temporarily unavailable"}}
```

So the frontend has exactly **one** error parser and switches on
`error.code`. `backend/tests/test_edge_contract.py` enforces this on the app
side — including a case found during verification, where an unmatched route
returned Starlette's default `{"detail":"Not Found"}` instead of our envelope.

---

## 17. Config quality gates

### `nginx -t` must pass

```bash
docker compose exec nginx nginx -t
```

```
nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
nginx: configuration file /etc/nginx/nginx.conf test is successful
```

It also runs **at image build time** — but not in the obvious way, and the
reason is instructive.

**Nginx resolves every upstream hostname at config-parse time** and aborts with
`host not found in upstream "api1:8000"` if one is unknown. At build time there
is no compose network, so a plain `nginx -t` always fails. (I hit this on the
first build.) Patching `/etc/hosts` is not an option either — BuildKit mounts it
read-only. So the Dockerfile validates a **copy** of the config with the
upstream hostnames rewritten to a literal IP:

```dockerfile
RUN /docker-entrypoint.d/10-gen-cert.sh \
 && mkdir -p /tmp/cfgcheck \
 && sed 's#server api[0-9]*:8000#server 127.0.0.1:8000#' /etc/nginx/nginx.conf > /tmp/cfgcheck/nginx.conf \
 && nginx -t -c /tmp/cfgcheck/nginx.conf \
 && rm -rf /tmp/cfgcheck
```

That still checks every directive, every include, every regex and the
certificate paths. The only thing it skips is the DNS lookup — precisely the
part that cannot work without the compose network.

**This same resolve-at-startup behaviour is why `docker-compose.yml` makes
`nginx` `depend_on` the three API services.** Without it, a cold
`docker compose up` would crash Nginx roughly half the time. It is also the
standard gotcha with dynamic backends in production: upstreams that come and go
need either a `resolver` plus a variable in `proxy_pass`, or service discovery
that rewrites the config.

### Zero-downtime reload

```bash
docker compose cp nginx/nginx.conf nginx:/etc/nginx/nginx.conf
docker compose exec nginx nginx -t          # ALWAYS test first
docker compose exec nginx nginx -s reload
```

```
nginx: configuration file /etc/nginx/nginx.conf test is successful
2026/10/22 06:22:16 [notice] 77#77: signal process started
```

**What happens on reload.** The master re-reads the config, starts **new**
workers with it, and tells the old workers to stop accepting new connections and
exit once their in-flight requests finish. No connection is dropped and no
request is lost. This is how the `zone api_backend 64k` fix and the
`Cache-Control` fix above were both applied to a running stack mid-session, with
traffic flowing.

**`nginx -t` before `-s reload`, always.** A reload with a broken config leaves
the old workers running (so the site stays up), but you have no idea the change
did not apply.

---

## 18. Quick reference — one command per feature

| Feature | Command |
|---|---|
| Config valid | `docker compose exec nginx nginx -t` |
| Zero-downtime reload | `docker compose exec nginx nginx -s reload` |
| HTTP → HTTPS | `curl -s -i http://localhost:8080/ \| head -5` |
| TLS version + ALPN | `echo \| openssl s_client -alpn h2,http/1.1 -connect localhost:8443 2>&1 \| strings \| grep -i alpn` |
| Round robin | `for i in $(seq 1 30); do curl -sk -D - -o /dev/null https://localhost:8443/api/health \| grep -i x-instance; done \| sort \| uniq -c` |
| Failover | `docker compose kill api2` then the round-robin command |
| Rejoin | `docker compose start api2 && sleep 12` then the same |
| Microcache | `curl -sk -D - -o /dev/null https://localhost:8443/api/events/1/seats \| grep -i x-cache` (twice) |
| Cache collapse | `for i in $(seq 1 60); do curl -sk -D - -o /dev/null .../seats \| grep -i x-cache & done \| sort \| uniq -c` |
| General rate limit | `for i in $(seq 1 120); do curl -sk -o /dev/null -w '%{http_code}\n' .../api/time & done \| sort \| uniq -c` |
| Login rate limit | 8× `curl -X POST .../api/auth/login` with a wrong password |
| Per-token limit | 12× `curl -H "Authorization: Bearer $A" .../api/holds/<uuid>`, then 5× with `$B` |
| 413 | `curl --data-binary @a-120kb-file .../api/holds` |
| gzip | `curl -sk -H 'Accept-Encoding: gzip' -o /dev/null -w '%{size_download}' .../seats` |
| Security headers | `curl -sk -D - -o /dev/null .../api/events/1/seats` |
| stub_status | `curl -sk https://localhost:8443/nginx_status` |
| JSON log | `docker compose exec nginx tail -1 //var/log/nginx/access.json.log` |

`scripts/verify.sh` runs the scriptable subset of these in one go.
