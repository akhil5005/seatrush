#!/bin/sh
# ===========================================================================
#  Plan B entrypoint: one container running Nginx + three uvicorn processes.
#
#  Order matters and is not arbitrary:
#     1. render the Nginx config  (needs $PORT, which Render sets at runtime)
#     2. apply the schema + seed  (the app must never start against no tables)
#     3. start three uvicorn processes on 127.0.0.1:8001/8002/8003
#     4. WAIT for /api/health on each one
#     5. exec nginx in the foreground
#
#  Step 4 is the one people skip. Nginx resolves and connects to its upstreams
#  at startup; if the uvicorn processes are not listening yet it logs
#  "connect() failed" and the first users get 502s. Waiting costs a second.
# ===========================================================================
set -eu

: "${PORT:=10000}"
: "${DATABASE_URL:?DATABASE_URL is required (point it at a managed Postgres)}"

echo "[start] rendering nginx config for PORT=$PORT"

# *** envsubst '${PORT}' -- THE QUOTED ARGUMENT IS LOAD-BEARING ***
# Without it, envsubst replaces EVERY $name in the file, including Nginx's own
# variables: $host, $remote_addr, $upstream_addr, $binary_remote_addr,
# $request_uri... all become empty strings. The config still PARSES, which is
# what makes it dangerous -- you get a site with no real client IPs, broken
# logging, and rate limiting keyed on "" (i.e. one shared bucket for everyone).
envsubst '${PORT}' \
  < /etc/nginx/nginx.render.conf.template \
  > /etc/nginx/nginx.conf

# Fail fast and loudly rather than serving a broken edge.
nginx -t

echo "[start] applying schema + seed"
cd /srv/app
# Fewer load-test fixtures than Plan A: a free managed Postgres has a small
# row/storage quota and the Render demo is for browsing, not load testing.
python -m app.seed --migrate --loadtest-users 20

echo "[start] starting three uvicorn processes"
start_api() {
  name="$1"; port="$2"
  INSTANCE_NAME="$name" \
  PEERS="http://127.0.0.1:8001,http://127.0.0.1:8002,http://127.0.0.1:8003" \
  NGINX_ACCESS_LOG="/tmp/nginx-access.json.log" \
  uvicorn app.main:app --host 127.0.0.1 --port "$port" --log-level warning &
  echo "[start]   $name on 127.0.0.1:$port (pid $!)"
}

start_api api1 8001
start_api api2 8002
start_api api3 8003

# --- step 4: wait until each replica actually answers ---------------------
for port in 8001 8002 8003; do
  i=0
  until wget -q -O /dev/null "http://127.0.0.1:$port/api/health" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -gt 60 ]; then
      echo "[start] FATAL: 127.0.0.1:$port never became healthy" >&2
      exit 1
    fi
    sleep 1
  done
  echo "[start]   127.0.0.1:$port healthy after ${i}s"
done

# If any uvicorn dies, take the whole container down so Render restarts it --
# a silently degraded container is worse than a restarting one.
monitor() {
  while true; do
    for port in 8001 8002 8003; do
      wget -q -O /dev/null "http://127.0.0.1:$port/api/health" 2>/dev/null || {
        echo "[start] replica on $port stopped answering; exiting so Render restarts us" >&2
        kill 1
      }
    done
    sleep 30
  done
}
monitor &

echo "[start] handing over to nginx on :$PORT"
# exec so Nginx becomes PID 1 and receives Render's SIGTERM directly,
# which is what makes shutdown graceful.
exec nginx -g 'daemon off;'
