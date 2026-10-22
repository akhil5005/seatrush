# ---------------------------------------------------------------------------
# Stage 1: build the React SPA.
# Node is only needed to BUILD; the result is static files, so Node never ships
# to production. This is the main reason for a multi-stage build here -- the
# final image is ~50 MB of Nginx + HTML instead of ~1.1 GB with node_modules.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /build
# Copy manifests first so `npm ci` is cached and only re-runs when deps change,
# not on every source edit.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build          # -> /build/dist

# ---------------------------------------------------------------------------
# Stage 2: Nginx.
# Pinned minor version: `http2 on;` (used in the TLS server block) only exists
# from 1.25.1, and the old `listen ... http2` form was deprecated in the same
# release. An unpinned `nginx:alpine` would silently change that syntax under us.
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine

# openssl is needed by the start-up hook that generates the self-signed cert.
RUN apk add --no-cache openssl

# Our config replaces the stock default.conf entirely.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/snippets/ /etc/nginx/snippets/

# The official image runs every executable /docker-entrypoint.d/*.sh before
# starting Nginx. Putting cert generation there is what makes
# `docker compose up --build` work with zero manual steps on a fresh clone.
COPY scripts/gen-cert.sh /docker-entrypoint.d/10-gen-cert.sh
RUN chmod +x /docker-entrypoint.d/10-gen-cert.sh

# The SPA build output, served directly by Nginx.
COPY --from=frontend /build/dist /usr/share/nginx/html

# Fail the BUILD if the config is invalid, so a typo can never reach a running
# container.
#
# Two prerequisites make this work, and both are instructive:
#  1. `nginx -t` reads ssl_certificate, so the cert must exist first -- hence
#     running the start-up hook here too (it is a no-op at run time because the
#     cert is then already in the mounted volume).
#  2. Nginx RESOLVES EVERY UPSTREAM HOSTNAME AT CONFIG-PARSE TIME and aborts
#     with "host not found in upstream" if one is unknown. At build time there
#     is no compose network, so api1/api2/api3 do not exist and a plain
#     `nginx -t` always fails. (Patching /etc/hosts is not an option either --
#     BuildKit mounts it read-only.) So we validate a COPY of the config with
#     the upstream hostnames rewritten to a literal IP. That still checks every
#     directive, every include, every regex and the certificate paths -- the
#     only thing it skips is the DNS lookup, which is precisely the part that
#     cannot work without the compose network.
#
# This same resolve-at-startup behaviour is why docker-compose.yml makes nginx
# depend_on the three API services, and why `scripts/verify.sh` runs the real
# `nginx -t` INSIDE the running container as well.
RUN /docker-entrypoint.d/10-gen-cert.sh \
 && mkdir -p /tmp/cfgcheck \
 && sed 's#server api[0-9]*:8000#server 127.0.0.1:8000#' /etc/nginx/nginx.conf > /tmp/cfgcheck/nginx.conf \
 && nginx -t -c /tmp/cfgcheck/nginx.conf \
 && rm -rf /tmp/cfgcheck

EXPOSE 80 443
