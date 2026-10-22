"""All runtime configuration in one place, read from environment variables.

Everything here has a development default so a new clone can run without a
.env file. Every default that would be a secret in production is marked
DEV-ONLY and is also overridden in docker-compose.yml.
"""

import os


def _int_env(name: str, default: int) -> int:
    """Read an int env var, falling back to the default if it is unset/blank."""
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


# Which replica am I? Stamped onto every response as the X-Instance header so
# the ops dashboard can show Nginx's round robin spreading traffic.
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "api-local")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://seatrush:seatrush@localhost:5432/seatrush",
)

# DEV-ONLY secret. In production this comes from the platform's secret store.
# The load test deliberately re-uses it to mint tokens locally (see loadtest/).
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-insecure-jwt-secret-change-me")
JWT_ALG = "HS256"
JWT_TTL_SECONDS = _int_env("JWT_TTL_SECONDS", 24 * 3600)

# How long a seat hold survives before it is considered expired. Tests override
# this to something tiny (e.g. 1 second) to exercise the expiry path.
HOLD_SECONDS = _int_env("HOLD_SECONDS", 300)

# Anti-hoarding limit: one user cannot grab the whole venue in one request.
MAX_SEATS_PER_HOLD = _int_env("MAX_SEATS_PER_HOLD", 6)

# Peers for GET /api/ops/cluster. Open-source Nginx has only *passive* health
# checks, so the app itself actively pings its siblings to render a health grid.
PEERS = [p.strip() for p in os.getenv("PEERS", "").split(",") if p.strip()]

# Nginx writes its JSON access log here; the API containers mount the same
# volume read-only so GET /api/ops/logs can tail it. Nginx is the only writer.
NGINX_ACCESS_LOG = os.getenv("NGINX_ACCESS_LOG", "/var/log/nginx/access.json.log")

# Size guard for the ops log reader (also enforced by Nginx's client_max_body_size
# on the way in, but this one bounds how much we are willing to read back out).
OPS_LOG_MAX_LIMIT = 200
