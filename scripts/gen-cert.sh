#!/bin/sh
# Self-signed TLS certificate for local HTTPS.
#
# Runs in two places with the same code:
#   * inside the Nginx image as /docker-entrypoint.d/10-gen-cert.sh (no args),
#     so `docker compose up` needs NO manual certificate step;
#   * on the host:  ./scripts/gen-cert.sh nginx/certs
#
# It is a no-op if a certificate already exists, so container restarts reuse
# the same key (otherwise the browser would warn about a new cert every time).
set -eu

CERT_DIR="${1:-${CERT_DIR:-/etc/nginx/certs}}"
DAYS="${DAYS:-365}"

mkdir -p "$CERT_DIR"
if [ -f "$CERT_DIR/server.crt" ] && [ -f "$CERT_DIR/server.key" ]; then
  echo "[gen-cert] certificate already present in $CERT_DIR, keeping it"
  exit 0
fi

echo "[gen-cert] generating a self-signed certificate in $CERT_DIR"
# -subj + -addext subjectAltName: modern browsers ignore CN entirely and look
# only at the SAN, so a cert without subjectAltName is rejected outright.
# 127.0.0.1 is included because some tools resolve localhost to the IP.
openssl req -x509 -nodes -newkey rsa:2048 \
  -days "$DAYS" \
  -keyout "$CERT_DIR/server.key" \
  -out    "$CERT_DIR/server.crt" \
  -subj "/C=IN/ST=Karnataka/L=Bengaluru/O=SeatRush Dev/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,DNS:seatrush.local,IP:127.0.0.1" \
  >/dev/null 2>&1

# The key must not be world-readable. Nginx reads it as root (the master
# process) before dropping privileges for the workers.
chmod 600 "$CERT_DIR/server.key"
chmod 644 "$CERT_DIR/server.crt"
echo "[gen-cert] done: $CERT_DIR/server.crt (self-signed, $DAYS days)"
