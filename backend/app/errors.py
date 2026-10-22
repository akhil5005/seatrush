"""One error shape for the whole API.

Every failure the frontend must react to is a stable machine-readable `code`.
The frontend switches on the code (seat_taken, sale_not_open, hold_expired,
rate_limited, ...) and never parses human text. Nginx's 429 page returns the
same shape so the client has exactly one error parser.
"""

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Raised anywhere in a request to produce {"error": {"code", "message"}}.

    Raising this *inside* an `async with connection.transaction()` block is the
    normal path for business-rule rejections: the exception unwinds the `async
    with`, asyncpg issues ROLLBACK, and every row we touched (including the
    idempotency-key row) disappears. That is deliberate -- a rejected request
    must leave no trace, so the client may retry the same action with the same
    Idempotency-Key.
    """

    def __init__(self, status_code: int, code: str, message: str, **details):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict:
        error = {"code": self.code, "message": self.message}
        error.update(self.details)
        return {"error": error}


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


async def unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Last resort: never leak a stack trace to the browser.

    Note this returns 500, and nginx.conf deliberately does NOT list http_500 in
    proxy_next_upstream: a 500 means the request *reached* the app and may have
    had side effects, so replaying it on another replica could double-book.
    """
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "Unexpected server error"}},
    )
