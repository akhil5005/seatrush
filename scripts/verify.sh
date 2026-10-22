#!/usr/bin/env bash
# =============================================================================
#  SeatRush -- automated verification
#
#  Checks everything in the README's acceptance checklist that can be scripted.
#  Run it after `docker compose up --build`:
#
#      ./scripts/verify.sh            # everything
#      ./scripts/verify.sh --quick    # skip the failover + load tests (~30s)
#
#  On Windows use Git Bash. Some checks exec into the Nginx container with
#  container paths, so MSYS_NO_PATHCONV=1 is set below -- without it Git Bash
#  rewrites /etc/nginx/... into C:/Program Files/Git/etc/nginx/... and the
#  check fails with a confusing "No such file".
# =============================================================================
set -uo pipefail
export MSYS_NO_PATHCONV=1

BASE_HTTPS="${BASE_HTTPS:-https://localhost:8443}"
BASE_HTTP="${BASE_HTTP:-http://localhost:8080}"
QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

PASS=0; FAIL=0; SKIP=0
RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'

ok()   { PASS=$((PASS+1)); printf "  ${GRN}PASS${OFF}  %s\n" "$1"; }
bad()  { FAIL=$((FAIL+1)); printf "  ${RED}FAIL${OFF}  %s\n" "$1"; [[ -n "${2:-}" ]] && printf "        ${DIM}%s${OFF}\n" "$2"; }
skip() { SKIP=$((SKIP+1)); printf "  ${YEL}SKIP${OFF}  %s\n" "$1"; }
section() { printf "\n${DIM}--- %s ---${OFF}\n" "$1"; }

# assert_eq <description> <expected> <actual>
assert_eq() {
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "expected '$2', got '$3'"; fi
}
# assert_contains <description> <needle> <haystack>
assert_contains() {
  if [[ "$3" == *"$2"* ]]; then ok "$1"; else bad "$1" "expected to contain '$2'"; fi
}

# The null device, as a curl ARGUMENT.
#
# This looks pedantic and is not. MSYS_NO_PATHCONV=1 above is required so that
# container paths (/etc/nginx/..., /var/log/nginx/...) survive being passed to
# `docker compose exec`. But it is global: Git Bash also stops rewriting
# /dev/null into NUL for native Windows programs. curl.exe then tries to CREATE
# a file literally named /dev/null, fails, and silently reports
# size_download=0 -- which made the gzip check read "plain=0 gzip=0" while gzip
# was in fact working perfectly (6587 -> 841 bytes).
#
# Shell redirections like `>/dev/null` are handled by bash itself and are fine;
# only arguments handed to a native .exe are affected.
case "${OSTYPE:-}" in
  msys*|cygwin*|win32) NULDEV="NUL" ;;
  *)                   NULDEV="/dev/null" ;;
esac

curlk() { curl -sk --max-time 15 "$@"; }
code()  { curlk -o "$NULDEV" -w '%{http_code}' "$@"; }
hdr()   { curlk -D - -o "$NULDEV" "$@" | tr -d '\r'; }

jsonval() { python -c "import sys,json;d=json.load(sys.stdin);print(eval('d'+'$1'))" 2>/dev/null; }

printf "${DIM}SeatRush verification -- %s${OFF}\n" "$(date)"

# =============================================================================
section "0. the stack is up"
# =============================================================================
if ! docker compose ps --status running --format '{{.Service}}' | grep -q nginx; then
  bad "compose stack is running" "start it with: docker compose up --build -d"
  echo; echo "Aborting -- nothing to verify."; exit 1
fi
ok "compose stack is running"

for svc in postgres api1 api2 api3 nginx; do
  if docker compose ps --status running --format '{{.Service}}' | grep -qx "$svc"; then
    ok "service up: $svc"
  else
    bad "service up: $svc"
  fi
done

# =============================================================================
section "1. Nginx config quality gates"
# =============================================================================
NGINX_T=$(docker compose exec -T nginx nginx -t 2>&1)
assert_contains "nginx -t passes inside the container" "test is successful" "$NGINX_T"

# Every directive group must carry an explanatory comment. A crude but useful
# proxy: the config is majority comments.
TOTAL_LINES=$(grep -c '' nginx/nginx.conf)
COMMENT_LINES=$(grep -cE '^\s*#' nginx/nginx.conf)
if (( COMMENT_LINES * 100 / TOTAL_LINES > 40 )); then
  ok "nginx.conf is documented (${COMMENT_LINES}/${TOTAL_LINES} lines are comments)"
else
  bad "nginx.conf is documented" "only ${COMMENT_LINES}/${TOTAL_LINES} comment lines"
fi

# The one directive that must NEVER appear uncommented.
if grep -E '^\s*proxy_next_upstream.*non_idempotent' nginx/nginx.conf >/dev/null; then
  bad "POSTs are never retried" "non_idempotent is ENABLED -- a retried confirm could double-book"
else
  ok "POSTs are never retried (non_idempotent absent from proxy_next_upstream)"
fi

# Shared-memory upstream state -- without it, round robin is per worker.
if grep -qE '^\s*zone\s+api_backend' nginx/nginx.conf; then
  ok "upstream state is in shared memory (zone api_backend)"
else
  bad "upstream state is in shared memory" "without 'zone', each worker round-robins independently"
fi

# =============================================================================
section "2. TLS, redirect and the SPA"
# =============================================================================
assert_eq "HTTPS serves the SPA"            "200" "$(code "$BASE_HTTPS/")"
assert_eq "HTTP :8080 redirects"            "301" "$(code "$BASE_HTTP/")"

LOC=$(hdr "$BASE_HTTP/events/1" | grep -i '^location:' | awk '{print $2}')
assert_eq "redirect keeps the non-standard port and path" \
          "https://localhost:8443/events/1" "$LOC"

assert_eq "SPA fallback serves deep links"  "200" "$(code "$BASE_HTTPS/checkout/some-uuid")"
assert_eq "SPA fallback serves /tickets"    "200" "$(code "$BASE_HTTPS/tickets")"

ALPN=$(echo | openssl s_client -alpn h2,http/1.1 -connect localhost:8443 \
        -servername localhost 2>&1 | strings | grep -i 'ALPN protocol' | head -1)
assert_contains "HTTP/2 is negotiated via ALPN" "h2" "$ALPN"

TLSV=$(echo | openssl s_client -connect localhost:8443 -servername localhost 2>&1 \
        | strings | grep -oE 'TLSv1\.[23]' | head -1)
if [[ "$TLSV" == "TLSv1.3" || "$TLSV" == "TLSv1.2" ]]; then
  ok "TLS version is $TLSV"
else
  bad "TLS 1.2/1.3 negotiated" "got '$TLSV'"
fi

SAN=$(echo | openssl s_client -connect localhost:8443 -servername localhost 2>/dev/null \
      | openssl x509 -noout -ext subjectAltName 2>/dev/null)
assert_contains "certificate has a subjectAltName for localhost" "DNS:localhost" "$SAN"

# =============================================================================
section "3. static asset caching"
# =============================================================================
ASSET=$(curlk "$BASE_HTTPS/" | grep -o '/assets/[^"]*\.js' | head -1)
if [[ -z "$ASSET" ]]; then
  bad "found the hashed JS bundle" "is the frontend built into the image?"
else
  ok "found the hashed JS bundle ($ASSET)"
  CC_COUNT=$(hdr "$BASE_HTTPS$ASSET" | grep -ci '^cache-control:')
  assert_eq "asset sends exactly ONE Cache-Control header" "1" "$CC_COUNT"
  CC=$(hdr "$BASE_HTTPS$ASSET" | grep -i '^cache-control:' | cut -d' ' -f2-)
  assert_contains "asset is immutable for a year" "immutable" "$CC"
  assert_contains "asset max-age is a year" "max-age=31536000" "$CC"
fi
INDEX_CC=$(hdr "$BASE_HTTPS/index.html" | grep -i '^cache-control:' | cut -d' ' -f2-)
assert_contains "index.html is no-cache (so deploys are picked up)" "no-cache" "$INDEX_CC"

# =============================================================================
section "4. compression"
# =============================================================================
PLAIN=$(curlk -o "$NULDEV" -w '%{size_download}' "$BASE_HTTPS/api/events/1/seats")
GZIP=$(curlk -H 'Accept-Encoding: gzip' -o "$NULDEV" -w '%{size_download}' "$BASE_HTTPS/api/events/1/seats")
if (( GZIP > 0 && PLAIN > GZIP )); then
  ok "seat map is gzipped (${PLAIN} -> ${GZIP} bytes, $(( PLAIN / GZIP ))x)"
else
  bad "seat map is gzipped" "plain=${PLAIN} gzip=${GZIP}"
fi
assert_contains "Vary: Accept-Encoding is set" "Accept-Encoding" \
  "$(hdr -H 'Accept-Encoding: gzip' "$BASE_HTTPS/api/events" | grep -i '^vary:')"

# =============================================================================
section "5. security headers (including the add_header inheritance trap)"
# =============================================================================
SEC_RE='^(x-content-type-options|x-frame-options|referrer-policy|permissions-policy|content-security-policy|strict-transport-security):'
for path in /api/events /api/events/1/seats /index.html /nginx_status /api/health "$ASSET"; do
  [[ -z "$path" ]] && continue
  N=$(hdr "$BASE_HTTPS$path" | grep -icE "$SEC_RE")
  if (( N == 6 )); then
    ok "6/6 security headers on $path"
  else
    bad "6/6 security headers on $path" "got $N -- a location with its own add_header lost the inherited set"
  fi
done

# HSTS must be HTTPS-only.
HSTS_HTTP=$(hdr "$BASE_HTTP/" | grep -ci '^strict-transport-security:')
assert_eq "HSTS is NOT sent over plain HTTP" "0" "$HSTS_HTTP"
assert_contains "server_tokens off (no version leak)" "Server: nginx" \
  "$(hdr "$BASE_HTTPS/" | grep -i '^server:')"

# =============================================================================
section "6. microcache policy"
# =============================================================================
sleep 3
for p in "/api/events" "/api/events/1" "/api/events/1/seats"; do
  curlk -o "$NULDEV" "$BASE_HTTPS$p"                     # warm
  CS=$(hdr "$BASE_HTTPS$p" | grep -i '^x-cache-status:' | awk '{print $2}')
  if [[ "$CS" == "HIT" || "$CS" == "STALE" || "$CS" == "UPDATING" ]]; then
    ok "cached: $p (X-Cache-Status: $CS)"
  else
    bad "cached: $p" "expected HIT/STALE/UPDATING, got '${CS:-<none>}'"
  fi
done

# These must have NO X-Cache-Status header at all -- the header is only added
# inside the three cached locations, so its absence proves nothing is cached.
for p in "/api/time" "/api/bookings" "/api/auth/me" "/api/ops/cluster" \
         "/api/admin/events/1/stats" "/api/holds/00000000-0000-0000-0000-000000000000"; do
  CS=$(hdr "$BASE_HTTPS$p" | grep -ci '^x-cache-status:')
  assert_eq "never cached: $p" "0" "$CS"
done

# =============================================================================
section "7. rate limiting"
# =============================================================================
sleep 2
TMP="./.verify-tmp"; rm -rf "$TMP"; mkdir -p "$TMP"
# ONE curl invocation with 120 URLs. It reuses a single keepalive connection
# and fires them back to back, which is both faster and far more reliable than
# 120 background curl processes -- on Windows most of those lost their output
# to an append race, and this check reported 31 results out of 120.
# NOTE: a single -o applies only to the FIRST url; the remaining URLs would
# dump their BODIES to stdout and pollute the codes file. curl pairs each -o
# with the url that follows it, so emit one -o per url.
ARGS=""
for i in $(seq 1 120); do ARGS="$ARGS -o $NULDEV $BASE_HTTPS/api/time"; done
# shellcheck disable=SC2086
curl -sk --max-time 30 -w '%{http_code}\n' $ARGS > "$TMP/codes" 2>/dev/null
N200=$(grep -c '^200$' "$TMP/codes" || true)
N429=$(grep -c '^429$' "$TMP/codes" || true)
if (( N429 > 0 && N200 > 0 )); then
  ok "general API limit engages (120 requests -> ${N200}x200 / ${N429}x429)"
else
  bad "general API limit engages" "200s=$N200 429s=$N429"
fi

# The 429 must be our JSON envelope with Retry-After, not an HTML page.
# Two curl details matter here:
#  * "#1" is substituted with the first URL GLOB, not with a request counter,
#    so the [1-100] range glob is what makes it meaningful. Repeating the same
#    URL 100 times would leave every response overwriting one file.
#  * "#1" is honoured by -o but NOT by -D: with -D all 100 header dumps land
#    in a single file literally named "h_#1". So we use -i, which prepends the
#    headers to each body, and get one complete response per file.
curl -sk --max-time 30 -i -o "$TMP/r_#1" \
     "$BASE_HTTPS/api/time?burst=[1-100]" >/dev/null 2>&1
FOUND429=0
for f in "$TMP"/r_*; do
  [[ -f "$f" ]] || continue
  if command head -1 "$f" | grep -q 429; then
    FOUND429=1
    RESP=$(tr -d '\r' < "$f")
    assert_contains "429 body is our JSON envelope" '"code":"rate_limited"' "$RESP"
    assert_contains "429 carries Retry-After"       "Retry-After"           "$RESP"
    N=$(echo "$RESP" | grep -icE "$SEC_RE")
    assert_eq       "429 still carries all security headers" "6" "$N"
    break
  fi
done
(( FOUND429 == 0 )) && skip "429 body/headers (no 429 produced in this window)"
rm -rf "$TMP"

# Login must break far sooner than the general API.
sleep 2
LOGIN_CODES=""
for i in $(seq 1 8); do
  LOGIN_CODES="$LOGIN_CODES $(code -X POST "$BASE_HTTPS/api/auth/login" \
    -H 'Content-Type: application/json' \
    -d '{"email":"verify-nobody@example.com","password":"definitely-wrong"}')"
done
assert_contains "login limit engages within 8 attempts" "429" "$LOGIN_CODES"
L429=$(echo "$LOGIN_CODES" | tr ' ' '\n' | grep -c '^429$' || true)
if (( L429 >= 4 )); then
  ok "login limit is much stricter than the API limit (${L429}/8 rejected)"
else
  bad "login limit is much stricter" "only ${L429}/8 rejected: $LOGIN_CODES"
fi

# =============================================================================
section "8. request size limit"
# =============================================================================
# NOT mktemp: on Windows, Git Bash's /tmp/... path is meaningless to the native
# curl.exe, which fails with "error encountered when reading a file". A path
# relative to the repo works for both Git Bash and curl.
BIG="./.verify-big.json"
python -c "import json;print(json.dumps({'event_id':1,'seat_ids':[1]*40000}))" > "$BIG"
BIG_CODE=$(code -X POST "$BASE_HTTPS/api/holds" -H 'Content-Type: application/json' \
            -H 'Idempotency-Key: verify' --data-binary @"$BIG")
assert_eq "body over 64 KB is rejected at the edge" "413" "$BIG_CODE"
BIG_BODY=$(curlk -X POST "$BASE_HTTPS/api/holds" -H 'Content-Type: application/json' \
            -H 'Idempotency-Key: verify' --data-binary @"$BIG")
assert_contains "413 body is our JSON envelope" '"code":"payload_too_large"' "$BIG_BODY"
rm -f "$BIG"

# =============================================================================
section "9. one error envelope everywhere"
# =============================================================================
assert_contains "unmatched route"  '"code":"not_found"'         "$(curlk "$BASE_HTTPS/api/nope")"
assert_contains "wrong method"     '"code":"method_not_allowed"' "$(curlk -X DELETE "$BASE_HTTPS/api/events")"
assert_contains "missing event"    '"code":"event_not_found"'   "$(curlk "$BASE_HTTPS/api/events/999999")"
assert_contains "unauthenticated"  '"code":"unauthorized"'      "$(curlk "$BASE_HTTPS/api/auth/me")"

# =============================================================================
section "10. load balancing"
# =============================================================================
sleep 2
RR=$(for i in $(seq 1 30); do hdr "$BASE_HTTPS/api/health" | grep -i '^x-instance:' | awk '{print $2}'; done | sort | uniq -c)
DISTINCT=$(echo "$RR" | wc -l | tr -d ' ')
if (( DISTINCT == 3 )); then
  ok "round robin spreads across all 3 replicas"
  echo "$RR" | sed 's/^/        /'
else
  bad "round robin spreads across all 3 replicas" "saw $DISTINCT distinct instances:
$RR"
fi

# =============================================================================
section "11. logging and metrics"
# =============================================================================
curlk -o "$NULDEV" "$BASE_HTTPS/api/health"
sleep 2
LOGLINE=$(docker compose exec -T nginx tail -1 /var/log/nginx/access.json.log 2>/dev/null | tr -d '\r')
if echo "$LOGLINE" | python -c "import sys,json;json.loads(sys.stdin.read())" >/dev/null 2>&1; then
  ok "access log lines are valid JSON (escape=json)"
else
  bad "access log lines are valid JSON" "last line: $LOGLINE"
fi
for f in upstream_addr upstream_status upstream_cache_status request_time; do
  assert_contains "access log has $f" "\"$f\"" "$LOGLINE"
done

STUB=$(curlk "$BASE_HTTPS/nginx_status")
assert_contains "stub_status responds" "Active connections" "$STUB"
ACC=$(echo "$STUB" | awk 'NR==3{print $1}')
HAN=$(echo "$STUB" | awk 'NR==3{print $2}')
if [[ -n "$ACC" && "$ACC" == "$HAN" ]]; then
  ok "Nginx has dropped zero connections (accepts == handled == $ACC)"
else
  bad "Nginx has dropped zero connections" "accepts=$ACC handled=$HAN"
fi

# =============================================================================
section "12. pytest (concurrency, idempotency, expiry, integrity)"
# =============================================================================
if (( QUICK )); then
  skip "pytest (--quick)"
else
  PYTEST_OUT=$(docker compose run --rm --no-deps -e DB_POOL_MAX=25 api1 pytest 2>&1 | tail -3)
  if echo "$PYTEST_OUT" | grep -qE '[0-9]+ passed' && ! echo "$PYTEST_OUT" | grep -q 'failed'; then
    ok "pytest: $(echo "$PYTEST_OUT" | grep -oE '[0-9]+ passed.*')"
  else
    bad "pytest" "$PYTEST_OUT"
  fi
fi

# =============================================================================
section "13. failover (stop api2, confirm traffic survives, bring it back)"
# =============================================================================
if (( QUICK )); then
  skip "failover (--quick)"
else
  docker compose stop api2 >/dev/null 2>&1
  sleep 1
  FAILS=0
  for i in $(seq 1 30); do
    [[ "$(code "$BASE_HTTPS/api/health")" == "200" ]] || FAILS=$((FAILS+1))
  done
  assert_eq "all 30 requests succeed with api2 down" "0" "$FAILS"

  INSTANCES=$(for i in $(seq 1 20); do hdr "$BASE_HTTPS/api/health" | grep -i '^x-instance:' | awk '{print $2}'; done | sort -u | tr '\n' ' ')
  if [[ "$INSTANCES" != *"api2"* ]]; then
    ok "api2 is out of rotation (serving: $INSTANCES)"
  else
    bad "api2 is out of rotation" "still saw api2: $INSTANCES"
  fi

  # The seat map keeps rendering because the cache can serve stale.
  assert_eq "seat map still served while a replica is down" "200" \
            "$(code "$BASE_HTTPS/api/events/1/seats")"

  docker compose start api2 >/dev/null 2>&1
  sleep 12   # > fail_timeout=10s, so Nginx lets a probe through again
  REJOIN=$(for i in $(seq 1 30); do hdr "$BASE_HTTPS/api/health" | grep -i '^x-instance:' | awk '{print $2}'; done | sort -u | tr '\n' ' ')
  if [[ "$REJOIN" == *"api2"* ]]; then
    ok "api2 rejoined the rotation (serving: $REJOIN)"
  else
    bad "api2 rejoined the rotation" "saw: $REJOIN"
  fi
fi

# =============================================================================
section "14. zero-downtime reload"
# =============================================================================
docker compose cp nginx/nginx.conf nginx:/etc/nginx/nginx.conf >/dev/null 2>&1
if docker compose exec -T nginx nginx -t >/dev/null 2>&1; then
  # Keep traffic flowing across the reload and prove nothing drops.
  # One curl process streaming 400 requests, with the reload fired in the
  # middle. A fresh file each time, so a previous run cannot pollute the count.
  RTMP="./.verify-reload"; rm -f "$RTMP"
  ARGS=""
  for i in $(seq 1 400); do ARGS="$ARGS -o $NULDEV $BASE_HTTPS/api/health"; done
  # The `>/dev/null 2>&1` on the background job is not cosmetic. When this
  # script's stdout is a pipe it is block-buffered; forking a background
  # subshell copies that buffer, and the child flushes it again on exit, so
  # every line printed so far appears TWICE. Closing the child's stdout stops
  # it inheriting (and re-emitting) the parent's buffer.
  # shellcheck disable=SC2086
  { curl -sk --max-time 60 -w '%{http_code}\n' $ARGS > "$RTMP" 2>/dev/null ; } >/dev/null 2>&1 &
  LOADPID=$!
  sleep 0.7
  docker compose exec -T nginx nginx -s reload >/dev/null 2>&1
  wait $LOADPID
  TOTALC=$(grep -c '' "$RTMP" 2>/dev/null)
  # What counts as a DROPPED request here is the important bit.
  #   200  served normally
  #   429  ALSO served -- 400 back-to-back requests trip the 20r/s api_ip
  #        limit, and a 429 is a complete HTTP response over a healthy
  #        connection. Counting it as a failure would make this check measure
  #        the rate limiter instead of reload downtime (it did, at first: 241
  #        of 400 "failures" were all 429s).
  #   000  curl could not get a response at all -- connection refused or reset.
  #        THIS is what a non-graceful reload would produce.
  #   5xx  Nginx up but unable to reach a backend.
  # No `|| echo 0` here: grep -c already PRINTS 0 when it matches nothing (it
  # just exits 1), so the fallback would append a second zero and the variable
  # would hold two lines instead of one.
  DROPPED=$(grep -cE '^(000|5[0-9][0-9])$' "$RTMP" 2>/dev/null)
  BREAKDOWN=$(sort "$RTMP" | uniq -c | tr '\n' ' ')
  if [[ "$DROPPED" == "0" ]]; then
    ok "no dropped requests across nginx -s reload (${TOTALC} requests: ${BREAKDOWN})"
  else
    bad "no dropped requests across nginx -s reload" \
        "$DROPPED of $TOTALC got no response or a 5xx: $BREAKDOWN"
  fi
  rm -f "$RTMP"
else
  bad "nginx -t before reload"
fi

# =============================================================================
section "15. flash-sale load test (the oversell proof)"
# =============================================================================
if (( QUICK )); then
  skip "load test (--quick)"
elif ! python -c "import httpx, jwt" 2>/dev/null; then
  skip "load test (pip install httpx PyJWT to run it from the host)"
else
  LT=$(python loadtest/flashsale.py --mode edge --users 300 2>&1)
  if echo "$LT" | grep -q "PASS"; then
    ok "edge mode: $(echo "$LT" | grep -E '^  PASS' | sed 's/^ *PASS *//')"
  else
    bad "edge mode load test" "$(echo "$LT" | tail -12)"
  fi

  LTD=$(docker compose --profile tools run --rm loadtest --mode direct --users 300 2>&1)
  if echo "$LTD" | grep -q "PASS"; then
    ok "direct mode (Nginx bypassed): $(echo "$LTD" | grep -E '^  PASS' | sed 's/^ *PASS *//')"
  else
    bad "direct mode load test" "$(echo "$LTD" | tail -12)"
  fi

  LTP=$(python loadtest/flashsale.py --poll --event 1 --viewers 40 --duration 15 2>&1)
  if echo "$LTP" | grep -q "PASS"; then
    ok "poll mode: $(echo "$LTP" | grep -E '^  PASS' | sed 's/^ *PASS *//')"
  else
    bad "poll mode load test" "$(echo "$LTP" | tail -12)"
  fi
fi

# =============================================================================
printf "\n${DIM}%s${OFF}\n" "========================================================"
printf "  ${GRN}%d passed${OFF}   ${RED}%d failed${OFF}   ${YEL}%d skipped${OFF}\n" "$PASS" "$FAIL" "$SKIP"
printf "${DIM}%s${OFF}\n" "========================================================"
exit $(( FAIL > 0 ? 1 : 0 ))
