# DEMO.md — the 6–8 minute screen share

Eight steps, in this order. Each one says **the exact command or click**, **what
the audience sees**, and **one sentence to say**. Recovery notes are at the end
of each step.

---

## Before you start (5 minutes ahead)

```bash
cd SeatRush
docker compose up --build -d
docker compose ps          # all five services up, postgres healthy
```

Then:

1. Open **https://localhost:8443** and click through the certificate warning
   **now**, not during the demo. (Chrome: *Advanced → Proceed*, or type
   `thisisunsafe` on the warning page.)
2. Log in as `admin@seatrush.dev` / `admin123` in one tab — that unlocks
   `/admin` and `/ops`.
3. Open a second browser tab on **https://localhost:8443/ops** and leave it there.
4. Have a terminal open in the repo root.
5. Note that the seed refreshes event times on every `up`, so **event 2 opens
   about 2 minutes after you start the stack** — handy, but it means you should
   start the stack *just* before the interview if you want to show the
   countdown hitting zero.

Nothing below needs the internet.

---

## 1. Book a seat, end to end  (~60 s)

**Do:** On `/`, click **Pick seats** on *Midnight Static – Live*. Click two
available seats. Click **Hold 2 seats** → you land on checkout with a counter
ticking down from 05:00. Click **Pay ₹2,998.00**. Click **My tickets**.

**They see:** a seat map, two seats turning blue, a countdown, a booking code
like `SR-8YACZ7`, then the ticket.

**Say:** *"That is the whole product. The interesting part is that those two
seats are now impossible for anybody else to get — and the countdown is running
on the server's clock, not my laptop's, because the sale gate is enforced in
Postgres with `now()`."*

> **If it goes wrong:** if the seats you click are already taken (I have been
> booking against this database), just pick two others — or hit **Reset** for
> that event on `/admin` first. If you get *"You already have seats on hold"*,
> the app sends you straight to your existing checkout; pay or release it.

---

## 2. Round robin  (~30 s)

**Do:** On the `/ops` tab, click **Send 30 requests**.

**They see:** three bars, roughly 10 / 10 / 10 across `api1`, `api2`, `api3`.

**Say:** *"Three identical FastAPI replicas behind Nginx. They are stateless —
auth is a JWT and all the state is in Postgres — so any replica can serve any
request, which is why plain round robin works with no sticky sessions."*

**The detail worth volunteering:** *"This was broken at first and it taught me
something. Round-robin position lives in each worker's private memory, and
`worker_processes auto` gave me 12 workers — so 30 separate connections all went
to api1. The fix is one line, `zone api_backend 64k`, which puts the
load-balancer state in shared memory."*

> **If it goes wrong:** if one replica shows 0, it is probably still stopped from
> a previous failover demo — `docker compose start api2`.

---

## 3. Microcache  (~60 s)

**Do:** Switch to the seat-map tab (`/events/1`). Point at the small text under
*Your selection*: `X-Cache-Status: HIT`. Reload once to show `MISS` → `HIT`.

Then in the terminal:

```bash
python loadtest/flashsale.py --poll --event 1 --viewers 40 --duration 20
```

**They see:** ~318 polls, **100% served from cache**, and the summary line.

Then show the honest backend-side count:

```bash
docker compose logs --since 1m api1 api2 api3 | grep -c "GET /api/events/1/seats"
```

**They see:** a number around **33** — for 318 polls.

**Say:** *"Every open seat map polls every 2.5 seconds. A thousand viewers would
be 400 requests a second asking the identical question. A one-second microcache
with `proxy_cache_lock` collapses that to about one backend request per second —
and that number stays flat whether 40 or 200 people are watching. The backend's
load stops depending on the size of the crowd."*

**The question they will ask — have the answer ready:** *"Is a stale seat map
not a correctness problem?"* → *"No, because the map is only a hint. Seat
ownership is decided exclusively by a conditional UPDATE in Postgres. The worst
case is you click a seat someone just took and get a clean 409 that highlights
it."*

> **If it goes wrong:** if `--poll` shows a lot of `RATE_LIMITED`, you used too
> many viewers for one source IP — that is the per-IP limiter, not a cache
> failure, and the tool says so in its output. Drop to `--viewers 40`.
> If `python` is missing, skip the script: reloading the page twice and showing
> `MISS` → `HIT` makes the point on its own.

---

## 4. Rate limits  (~45 s)

**Do:** On `/ops`, click **Spam 60 requests**.

**They see:** roughly **42 × 200** and **18 × 429**, plus `Retry-After: 1s`.

**Say:** *"Sixty requests at once against a 20-per-second limit with a burst of
40. The 429s never reach a backend — Nginx answers them itself, in the same JSON
envelope the API uses, so the frontend has exactly one error parser."*

**Then show the stricter one.** In the terminal:

```bash
for i in $(seq 1 8); do
  curl -sk -o /dev/null -w '%{http_code} ' -X POST https://localhost:8443/api/auth/login \
    -H 'Content-Type: application/json' -d '{"email":"nobody@x.io","password":"wrong"}'
done; echo
```

**They see:** `401 401 401 401 429 429 429 429`

**Say:** *"Login is five per **minute**, not per second — a thousand times
stricter — because every attempt costs us a 50-millisecond scrypt hash, so
brute force is expensive for us as well as for the attacker."*

**The one I am proudest of, if there is time:** the booking limit is keyed on
the JWT, not the IP. *"A per-IP limit is useless when four hundred students
share one college NAT, and too loose when one person scripts fifty attempts from
one laptop. I measured it: token A got rate-limited while token B, from the same
IP at the same instant, sailed through."*

> **If it goes wrong:** if you see no 429s, the bucket has drained — click the
> button twice in quick succession.

---

## 5. The flash sale  (~90 s — the centrepiece)

**Do:**

```bash
docker compose --profile tools run --rm loadtest --mode direct --users 300
```

**They see:**

```
300 users, 10 seats, 1.48s wall time
  201   9    hold created
  409 291    lost the race, handled cleanly
SEATS IN TWO BOOKINGS : 0
PASS  10 seats, 10 booked, 0 oversold.
```

**Say:** *"Three hundred users, ten seats, all at the same instant — and this
mode goes **straight to the replicas, bypassing Nginx entirely**. No rate
limiting, no caching, nothing protecting the database. Ten seats, ten bookings,
zero oversold, and the 291 who lost got a clean 409 telling them which seat
went."*

**Why there are two modes — say this, it is the point:** *"I test edge
protection and data correctness separately on purpose. If I only ran through the
rate limiter, the test could pass because the limiter throttled the race away
rather than because the locking is right."*

If there is time, run `--mode edge` too and contrast the 429 count.

**Then show where the guarantee actually lives.** Open `backend/app/holds.py`
at `claim_seats` and point at the SQL:

```sql
UPDATE seats SET status = 'held', hold_id = $1
 WHERE event_id = $2 AND id = ANY($3::bigint[])
   AND (status = 'available'
        OR (status = 'held' AND hold_id IN (
              SELECT id FROM holds WHERE status='active' AND expires_at < now())))
RETURNING id
```

*"The check and the write are the same statement, so there is no window between
them. Two transactions hitting the same row: the second blocks on the row lock,
and when the first commits it re-evaluates its WHERE clause against the new row
version and matches zero rows. If the count coming back is smaller than what was
asked for, the whole transaction rolls back — so a multi-seat hold is
all-or-nothing."*

And the backstop, in `schema.sql`: `UNIQUE (seat_id)` on `booking_seats`.
*"Defence in depth. The logic makes overselling impossible in practice; that
constraint makes it impossible even if the logic has a bug."*

> **If it goes wrong:** if the event is already sold out from a previous run, the
> load test creates a **fresh** event every time, so just run it again. If the
> admin login 429s, wait 60 seconds — the tool tells you that explicitly.

---

## 6. Failover  (~60 s)

**Do:** With the seat-map tab visible and polling:

```bash
docker compose stop api2
```

Watch the `/ops` tab: the api2 card turns red within ~3 seconds. The seat map
keeps updating. Book a seat if you like.

```bash
docker compose start api2
```

After ~12 seconds, click **Send 30 requests** again → back to 10/10/10.

**Say:** *"A replica just died and the site did not notice. Nginx's health
checks here are **passive** — open-source Nginx has no active prober, that is an
Nginx Plus feature — so it learns api2 is gone when a real request to it fails,
and `proxy_next_upstream` retries that request on a healthy replica."*

**Show the evidence** (this lands well):

```bash
docker compose exec nginx sh -c "grep '8000, ' /var/log/nginx/access.json.log | tail -1"
```

```json
{"status":200,"request_time":3.005,
 "upstream_addr":"172.21.0.4:8000, 172.21.0.5:8000",
 "upstream_status":"504, 200"}
```

*"Two upstreams on one request: it tried the dead replica, timed out after the
three-second connect timeout, retried, and the user got a 200 and never knew.
That is also why the log treats `upstream_status` as a **string** — after a retry
it is a comma-separated list, and treating it as a number is a bug that only
shows up during a failover."*

**And the thing we deliberately do NOT do:** *"POSTs are never retried. There is
no `non_idempotent` in `proxy_next_upstream`, and there never will be — a
timed-out-but-actually-succeeded payment, replayed, is a double booking."*

> **If it goes wrong:** api2 needs > 10 s (`fail_timeout`) after restart before
> Nginx lets traffic back to it — wait, do not panic. If the ops grid does not
> go red, confirm the container really stopped with `docker compose ps`.

---

## 7. Zero-downtime config reload  (~45 s)

**Do:** Edit `nginx/nginx.conf` — change the general limit from `20r/s` to
`5r/s`:

```nginx
limit_req_zone $binary_remote_addr zone=api_ip:10m rate=5r/s;
```

Then:

```bash
docker compose cp nginx/nginx.conf nginx:/etc/nginx/nginx.conf
docker compose exec nginx nginx -t        # ALWAYS test first
docker compose exec nginx nginx -s reload
```

Click **Spam 60 requests** on `/ops` → far more 429s than before.

**Say:** *"`nginx -t` first, always. On reload the master starts new workers with
the new config and tells the old ones to finish their in-flight requests and
exit — no connection is dropped. I verified that: 400 requests streaming across
a reload, zero of them failed."*

Change it back and reload again.

> **If it goes wrong:** if `nginx -t` fails, you have a typo — the old workers
> are still running the old config, so **the site is still up**. Fix the file and
> copy it again. That is exactly why you test before reloading.

---

## 8. The ops dashboard  (~45 s)

**Do:** Scroll the `/ops` tab to **Live requests**.

**They see:** a live table — time, method, URI, status, which upstream served it,
cache status, duration.

**Say:** *"This is Nginx's own JSON access log, tailed by the API. Note the rows
where the upstream column is empty — those were served entirely from the cache
and no backend was involved. And `/assets/` is missing entirely because
`access_log off` keeps this view about API traffic."*

Point at **Nginx status**: *"`accepts` minus `handled` is the number of
connections Nginx dropped because it hit a resource limit. It is zero, which is
the first number I would look at if the site felt slow."*

Point at **Cluster health**: *"And this exists because of a real limitation —
open-source Nginx can't actively probe. So the app probes its own siblings and
the page says so rather than pretending."*

---

## Closing line

*"Two stories: Nginx doing every job a gateway should do, each one with a reason
that is specific to this app — and a booking path that cannot oversell a seat,
proved by three hundred users fighting over ten of them with the edge switched
off."*

---

## Quick reference card

| # | Command / click |
|---|---|
| 1 | browse → hold → **Pay** → **My tickets** |
| 2 | `/ops` → **Send 30 requests** |
| 3 | `python loadtest/flashsale.py --poll --event 1 --viewers 40 --duration 20` |
| 4 | `/ops` → **Spam 60 requests**; then the 8× login loop |
| 5 | `docker compose --profile tools run --rm loadtest --mode direct --users 300` |
| 6 | `docker compose stop api2` … `docker compose start api2` |
| 7 | edit rate → `cp` → `nginx -t` → `nginx -s reload` |
| 8 | `/ops` → Live requests + Nginx status |

**Universal recovery:** `docker compose restart` fixes almost anything in ~15 s.
`docker compose down -v && docker compose up --build -d` is the nuclear option
(~2 min, wipes the database). `/admin` → **Reset** gives any event a clean venue
without touching anything else.
