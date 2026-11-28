"""``POST /client-errors``: error reports from the browser code.

The frontend and the Book Portal extension can't write files, so they send
each error here as one event of the shared format without ``ts`` and
``host``; the backend adds both and appends the event to
``client-errors.jsonl`` (``ibp/error_log.py``).

- The body is at most 16 KiB (larger is a 413) and must be a valid event
  (else a 422); ``app`` is ``frontend`` or ``book-portal-extension``.
- ``host`` is the client address nginx passes in ``X-Real-IP`` (the
  connection's address without it).
- At most 30 reports a minute are taken from one address. Over that they
  are dropped with a 429, and one WARNING per address and minute says how
  many were.
"""

import asyncio
import ipaddress
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .base import app
from .error_log import client_error_log, timestamp

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024
RATE_LIMIT = 30
RATE_WINDOW_S = 60.0

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ClientErrorReport(BaseModel):
    """One browser event; any other field (``ts``, ``host``) is ignored."""

    model_config = ConfigDict(extra="ignore")

    level: Level
    app: Literal["frontend", "book-portal-extension"]
    version: str | None = Field(default=None, max_length=100)
    logger: str | None = Field(default=None, max_length=200)
    msg: str
    exc: str | None = None
    data: dict[str, Any] | None = None

    @field_validator("level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value


@dataclass
class _Window:
    start: float
    count: int = 0
    dropped: int = 0


class RateLimiter:
    """At most ``limit`` reports per address in each ``window_s`` window.

    Each address's window starts at its first report. When a window that
    dropped reports ends, one WARNING says how many: on the next report
    from any address, or from a timer set at the first drop, whichever
    comes first.
    """

    def __init__(
        self,
        limit: int = RATE_LIMIT,
        window_s: float = RATE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._windows: dict[str, _Window] = {}

    def allow(self, address: str) -> bool:
        """Count a report from ``address``; False if it is over the limit."""
        now = self.clock()
        self.flush(now)
        window = self._windows.setdefault(address, _Window(start=now))
        if window.count < self.limit:
            window.count += 1
            return True
        window.dropped += 1
        if window.dropped == 1:
            self._schedule_flush(window.start + self.window_s - now)
        return False

    def flush(self, now: float | None = None) -> None:
        """Forget ended windows, reporting the drops of each."""
        now = self.clock() if now is None else now
        ended = [
            (address, window)
            for address, window in self._windows.items()
            if now - window.start >= self.window_s
        ]
        for address, window in ended:
            del self._windows[address]
            if window.dropped:
                logger.warning(
                    "dropped %d client error reports from %s over the limit "
                    "of %d a minute",
                    window.dropped,
                    address,
                    self.limit,
                    extra={"data": {"address": address, "dropped": window.dropped}},
                )

    def _schedule_flush(self, delay: float) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (a test calling allow() directly)
            return
        loop.call_later(max(delay, 0.0) + 0.1, self.flush)


rate_limiter = RateLimiter()


def client_address(request: Request) -> str:
    """The client's address: nginx's ``X-Real-IP``, else the connection's."""
    header = request.headers.get("x-real-ip", "").strip()
    if header:
        try:
            return str(ipaddress.ip_address(header))
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


async def read_body(request: Request) -> bytes | None:
    """The body, or None if it is over ``MAX_BODY_BYTES``."""
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > MAX_BODY_BYTES:
                return None
        except ValueError:
            pass
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


@app.post(
    "/client-errors",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": ClientErrorReport.model_json_schema()}
            },
        }
    },
)
async def report_client_error(request: Request) -> Response:
    """Record one error from the browser code (frontend, extension)."""
    received = time.time()
    address = client_address(request)
    if not rate_limiter.allow(address):
        return Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    body = await read_body(request)
    if body is None:
        return Response(
            content=json.dumps({"detail": "Report larger than 16 KiB."}),
            media_type="application/json",
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )
    try:
        report = ClientErrorReport.model_validate_json(body)
    except ValidationError as error:
        raise RequestValidationError(error.errors(include_url=False)) from error

    event: dict[str, Any] = {
        "ts": timestamp(received),
        "level": report.level,
        "app": report.app,
        "version": report.version,
        "logger": report.logger,
        "host": address,
        "msg": report.msg,
        "exc": report.exc,
        "data": report.data,
    }
    client_error_log.write({k: v for k, v in event.items() if v is not None})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
