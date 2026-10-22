"""FastAPI application wiring.

Everything is mounted under /api so that nginx.conf can `proxy_pass` to the
upstream WITHOUT a trailing slash and without any rewrite rules -- the URI the
browser sent is the URI the app sees. (The classic trap: `proxy_pass
http://api_backend/` with a trailing slash replaces the matched location prefix,
so /api/events would arrive as /events and 404. Keeping the /api prefix in the
app means there is nothing to rewrite and nothing to get wrong.)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import admin, auth, bookings, config, db, events, holds, ops
from .errors import ApiError, api_error_handler, unhandled_handler


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # One pool per process, created on startup and closed on shutdown. The
    # `migrate` service has already created the schema by the time uvicorn
    # starts (compose depends_on: service_completed_successfully).
    await db.connect()
    try:
        yield
    finally:
        await db.disconnect()


app = FastAPI(
    title="SeatRush API",
    version="1.0.0",
    lifespan=lifespan,
    # No CORS middleware anywhere: the browser talks to Nginx, and Nginx serves
    # the React build and proxies /api from the SAME origin. Same-origin means
    # no preflight requests, no CORS config to get wrong, and no credentials
    # leaking to a third origin.
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


@app.middleware("http")
async def stamp_instance(request: Request, call_next):
    """Tag every response with which replica produced it.

    This is what makes Nginx's round robin *visible* in the demo: the ops page
    fires 30 requests and counts the X-Instance values. It is a debug header --
    in production it would leak topology, so it would go.
    """
    response = await call_next(request)
    response.headers["X-Instance"] = config.INSTANCE_NAME
    return response


app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(Exception, unhandled_handler)


@app.exception_handler(RequestValidationError)
async def validation_handler(_request: Request, exc: RequestValidationError):
    """Reshape pydantic's 422 into our single error envelope."""
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "body"
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "invalid_request",
            "message": f"{field}: {first.get('msg', 'invalid value')}",
        }},
    )


app.include_router(auth.router)
app.include_router(events.router)
app.include_router(holds.router)
app.include_router(bookings.router)
app.include_router(admin.router)
app.include_router(ops.router)


@app.get("/api/time")
async def server_time():
    """Authoritative clock for the frontend countdowns.

    The sale gate is enforced with Postgres' now(), so the UI must count down
    to the same clock. The frontend stores (server_time - client_time) once and
    applies that offset -- otherwise a user with a skewed laptop clock sees
    "On sale" while every booking attempt returns 403 sale_not_open.

    This endpoint is NOT cached by Nginx; a cached clock is a wrong clock.
    """
    row = await db.pool().fetchrow("SELECT now() AS now")
    return {
        "now": row["now"],
        "epoch_ms": int(row["now"].timestamp() * 1000),
        "instance": config.INSTANCE_NAME,
    }
