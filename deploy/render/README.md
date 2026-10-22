# Plan B — Render

Render does not run docker-compose, so this is **one web-service container**
(Nginx + three uvicorn processes) plus a managed Postgres.

```
  Render edge (real TLS)   ┌────────── one web service ──────────┐
            │              │  Nginx :$PORT                       │
  https://…onrender.com ───┼─▶  /      → React build             │
                           │    /api/* → 127.0.0.1:8001/2/3      │
                           │              uvicorn ×3             │
                           └──────────────┬──────────────────────┘
                                          ▼
                                 Render PostgreSQL
```

## Deploy

1. Push the repo to GitHub.
2. Render → **New → Blueprint** and point at `deploy/render/render.yaml`,
   *or* **New → Web Service → Docker** with:
   - Dockerfile path: `deploy/render/Dockerfile`
   - Docker build context: `.` (the repo root — stage 1 needs `./frontend`)
3. Set `DATABASE_URL` (the blueprint wires it automatically) and let Render
   generate `JWT_SECRET`.
4. Health check path: `/api/health`.

## Build and smoke-test locally first

```bash
docker build -f deploy/render/Dockerfile -t seatrush-render .

docker run --rm -p 10000:10000 \
  -e PORT=10000 \
  -e DATABASE_URL='postgresql://seatrush:seatrush@host.docker.internal:5433/seatrush' \
  -e JWT_SECRET='local-test-secret' \
  seatrush-render
```

(For the `DATABASE_URL` above, uncomment the `5433:5432` port mapping on the
`postgres` service in the root `docker-compose.yml` so the Plan A database is
reachable from the host.)

Then:

```bash
curl -s localhost:10000/api/health
curl -s localhost:10000/api/events | head -c 200
curl -sI localhost:10000/ | head -5
```

## The four differences from Plan A

| | Plan A | Plan B | Why |
|---|---|---|---|
| Replicas | 3 containers `api1:8000` … | 3 processes `127.0.0.1:8001-8003` | One container per Render web service |
| Port | fixed 443/80 | `listen ${PORT};` | Render assigns it at runtime |
| TLS | we terminate it, self-signed | **Render terminates it** — no `ssl_*`, no redirect | Their edge, their certificate |
| Client IP | `$remote_addr` is the client | `real_ip_header X-Forwarded-For` + `set_real_ip_from` | Otherwise every visitor shares one rate-limit bucket |

Everything else — rate limit zones, microcache, security headers, the JSON log
format, the error envelope — is identical.

## The `envsubst` trap

`start.sh` renders the template with:

```sh
envsubst '${PORT}' < nginx.render.conf.template > /etc/nginx/nginx.conf
```

**The quoted `'${PORT}'` argument is load-bearing.** `envsubst` with no
argument substitutes *every* `$name` it finds — which would replace Nginx's own
variables (`$host`, `$remote_addr`, `$upstream_addr`, `$binary_remote_addr`,
`$request_uri`, …) with empty strings.

The dangerous part is that the result still **parses**. You would get a running
site with no client IPs in the logs, and `limit_req_zone $binary_remote_addr`
keyed on an empty string — one shared bucket for the entire internet. Limiting
substitution to the single variable we actually want is the whole fix, and
`nginx -t` runs immediately afterwards as a second net.

## Known limitations

- **Cold starts.** A free web service sleeps after inactivity; the first
  request can take up to a minute. Open the site a few minutes before a demo.
- **No shell on the free tier**, so `docker stop api2` has no equivalent — the
  failover demo must be done in Plan A.
- **Free Postgres quotas and expiry.** Check Render's current terms; free
  database instances have historically expired after a fixed window.
  Alternatives with free tiers: Neon, Supabase, Aiven.
- **Ephemeral disk**, so the Nginx cache and access log do not survive a
  restart. Fine — both are caches by nature.
- Seeded with 20 load-test fixtures instead of 300, to respect a small free
  storage quota.
