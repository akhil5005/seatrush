# ===========================================================================
#  SeatRush -- Plan B image (Render)
#
#  BUILD CONTEXT IS THE REPO ROOT, not this directory:
#      docker build -f deploy/render/Dockerfile -t seatrush-render .
#  because stage 1 needs ./frontend and stage 2 needs ./backend.
#
#  Plan A splits the work across two images (nginx + backend) because compose
#  can run many containers. Render's web service is ONE container, so this
#  image has to contain both -- Nginx plus three uvicorn processes. That is
#  the only structural difference between the two plans.
# ===========================================================================

# ---------------------------------------------------------------------------
# Stage 1: build the React SPA. Node never ships.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: Python + Nginx in one image.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# nginx        : the gateway
# gettext-base : provides envsubst, used to render ${PORT} into the config
# wget         : used by start.sh to wait for each replica's /api/health
RUN apt-get update \
 && apt-get install -y --no-install-recommends nginx gettext-base wget \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app

# The SPA, served straight off disk by Nginx.
COPY --from=frontend /build/dist /srv/www

# The config TEMPLATE (rendered at start-up) plus the shared snippets, which
# are byte-identical to Plan A -- the security headers and proxy headers do
# not change just because the deployment shape did.
COPY deploy/render/nginx.render.conf.template /etc/nginx/nginx.render.conf.template
COPY nginx/snippets/ /etc/nginx/snippets/
COPY deploy/render/start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh \
 && rm -f /etc/nginx/sites-enabled/default \
 && mkdir -p /tmp/nginx-cache

# Render injects $PORT at runtime; 10000 is its conventional default and is
# only here so `docker run` works locally without extra flags.
ENV PORT=10000
EXPOSE 10000

CMD ["/usr/local/bin/start.sh"]
