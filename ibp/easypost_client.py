"""A GET-only EasyPost client for the shipment sync.

Failures are reported in text safe to log (the sync's output goes to
Healthchecks): a fixed operation label, the HTTP status, a known EasyPost
error code, or an exception's class name, never a response body, an id or a
query string.
"""

import base64
import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, Optional

logger = logging.getLogger("ibp.shipment_sync")

API = "https://api.easypost.com/v2"
# Seconds between EasyPost requests, and the retry policy for 429 and 5xx.
PACE_SECONDS = 0.3
RETRIES = 6
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class ApiError(RuntimeError):
    """An EasyPost request failed."""


Opener = Callable[..., Any]


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Fails every redirect, as an ``HTTPError`` with its 3xx status:
    following one would send the key's ``Authorization`` header on."""

    def redirect_request(
        self, req, fp, code, msg, headers, newurl
    ):  # pylint: disable=too-many-arguments,too-many-positional-arguments
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def build_opener() -> Opener:
    """``urlopen`` for EasyPost: the standard one, but never redirected."""
    return urllib.request.build_opener(RefuseRedirects).open


class EasyPost:  # pylint: disable=too-few-public-methods
    """A GET-only EasyPost client, paced, retrying 429, 5xx, network errors
    and unreadable responses with backoff; a request that still fails raises
    ``ApiError``.

    A redirect is not followed: it fails as ``HTTP 3xx``. ``urlopen``
    (``build_opener()`` by default) and ``sleep`` are for tests.
    """

    def __init__(
        self,
        key: str,
        urlopen: Optional[Opener] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._auth = base64.b64encode(f"{key}:".encode()).decode()
        self._urlopen = urlopen if urlopen is not None else build_opener()
        self._sleep = sleep
        self.requests = 0

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict:
        """GET ``path`` and return its JSON, retrying transient failures.

        Warnings and errors name the request by ``operation`` only, never
        its id or query string.
        """
        what = operation(path)
        url = f"{API}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Basic {self._auth}",
                "User-Agent": "ibp-shipment-sync",
            },
        )
        for attempt in range(RETRIES):
            if self.requests:
                self._sleep(PACE_SECONDS)
            self.requests += 1
            try:
                with self._urlopen(request, timeout=60) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in RETRY_STATUSES or attempt == RETRIES - 1:
                    code = error_code(error)
                    raise ApiError(
                        f"{what}: HTTP {int(error.code)}" + (f" {code}" if code else "")
                    ) from error
                wait = retry_after(error) or 2.0**attempt
                logger.warning(
                    "%s: HTTP %d, retrying in %ss", what, int(error.code), wait
                )
                self._sleep(wait)
            except urllib.error.URLError as error:
                # The reason can be text naming the host or quoting a reply.
                reason = (
                    type(error.reason).__name__
                    if isinstance(error.reason, BaseException)
                    else type(error).__name__
                )
                if attempt == RETRIES - 1:
                    raise ApiError(f"{what}: {reason}") from error
                logger.warning("%s: %s, retrying", what, reason)
                self._sleep(2.0**attempt)
            except (OSError, http.client.HTTPException, ValueError) as error:
                # A read timeout, a dropped or truncated body, or bad JSON.
                failure = type(error).__name__
                if attempt == RETRIES - 1:
                    raise ApiError(f"{what}: {failure} reading response") from error
                logger.warning("%s: %s reading response, retrying", what, failure)
                self._sleep(2.0**attempt)
        raise ApiError(f"{what}: giving up after retries")


# The requests the sync makes, by the path's first part and whether an id
# follows it.
_OPERATIONS = {
    ("shipments", False): "GET shipments",
    ("shipments", True): "GET shipment",
    ("trackers", True): "GET tracker",
}


def operation(path: str) -> str:
    """A fixed label for a request (``GET tracker`` for ``trackers/trk_1``),
    safe to log: no id, no query string."""
    first, _, rest = path.partition("/")
    return _OPERATIONS.get((first, bool(rest)), "GET other")


# EasyPost error codes logged as they are; any other is logged as "other".
KNOWN_ERROR_CODES = frozenset(
    {
        "ADDRESS.VERIFY.FAILURE",
        "APIKEY.INACTIVE",
        "APIKEY.REQUIRED",
        "BAD_REQUEST",
        "FORBIDDEN",
        "INTERNAL_SERVER_ERROR",
        "METHOD_NOT_ALLOWED",
        "MODE.UNAUTHORIZED",
        "NOT_ACCEPTABLE",
        "NOT_FOUND",
        "PARAMETER.INVALID",
        "PARAMETER.REQUIRED",
        "PARAMETER.UNACCEPTED",
        "PAYMENT_REQUIRED",
        "RATE_LIMITED",
        "SERVICE_UNAVAILABLE",
        "TIMEOUT",
        "UNAUTHORIZED",
        "UNPROCESSABLE_ENTITY",
    }
)


def error_code(error: urllib.error.HTTPError) -> Optional[str]:
    """EasyPost's error ``code`` from an error response's JSON body: the
    code itself if it is one of ``KNOWN_ERROR_CODES`` (e.g. ``NOT_FOUND``),
    ``other`` for any other code, None for none.

    Nothing else of the body is kept: its message and field errors can quote
    an address, an unknown code could be anything, and the run's output goes
    to Healthchecks.
    """
    try:
        body = json.loads(error.read())
    except (OSError, ValueError, http.client.HTTPException):
        return None
    detail = body.get("error") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    if code is None:
        return None
    return code if code in KNOWN_ERROR_CODES else "other"


def retry_after(error: urllib.error.HTTPError) -> Optional[float]:
    """The seconds a ``Retry-After`` header asks for (capped at 60), if any."""
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        return min(float(value), 60.0) if value else None
    except ValueError:
        return None
