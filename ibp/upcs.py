"""UPC -> ISBN lookup, for books whose barcode is a retail UPC-A.

Some mass-market paperbacks carry a UPC-A (often with a 2- or 5-digit add-on)
instead of the ISBN barcode. UPCitemdb maps many UPCs to an ISBN; the
endpoint then does the normal ISBN lookup (see ``ibp.book_api``).

UPCitemdb's free trial endpoint needs no key but allows 100 requests a day;
with ``[books] upcitemdb_key`` set, the paid endpoint is used instead.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

from .base import config
from .books import DEFAULT_TIMEOUT, USER_AGENT, normalize_isbn, setting

logger = logging.getLogger(__name__)

TRIAL_URL = "https://api.upcitemdb.com/prod/trial/lookup"
PAID_URL = "https://api.upcitemdb.com/prod/v1/lookup"

# UPC-A plus an optional 2- or 5-digit add-on.
CODE_LENGTHS = (12, 14, 17)


class UpcServiceUnavailable(Exception):
    """UPCitemdb is rate-limited or unreachable; the answer must not be cached."""


def upc_check_digit_ok(upc: str) -> bool:
    """Whether a 12-digit UPC-A's check digit is right (weights 3,1,3,1...)."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(upc))
    return total % 10 == 0


def valid_code(code: str) -> bool:
    """A UPC-A (valid check digit) with an optional 2- or 5-digit add-on."""
    return (
        re.fullmatch(r"\d+", code) is not None
        and len(code) in CODE_LENGTHS
        and upc_check_digit_ok(code[:12])
    )


def has_add_on(code: str) -> bool:
    """Whether a valid code carries an add-on after the 12-digit UPC."""
    return len(code) > 12


def item_isbn(item: dict[str, Any]) -> Optional[str]:
    """The ISBN-13 of one UPCitemdb item, from ``isbn`` or a 978/979 ``ean``."""
    if isbn := normalize_isbn(str(item.get("isbn") or "")):
        return isbn
    ean = str(item.get("ean") or "")
    if re.fullmatch(r"97[89]\d{10}", ean):
        return normalize_isbn(ean)
    return None


def first_isbn(payload: dict[str, Any]) -> Optional[str]:
    """The ISBN of the first item in a UPCitemdb response that has one."""
    for item in payload.get("items") or []:
        if isinstance(item, dict) and (isbn := item_isbn(item)):
            return isbn
    return None


async def upcitemdb(upc: str) -> Optional[str]:
    """Ask UPCitemdb for the ISBN-13 of a 12-digit UPC; None if it has none.

    Raises UpcServiceUnavailable when rate-limited, failing or unreachable.
    """
    key = setting("upcitemdb_key")
    url, headers = TRIAL_URL, {"User-Agent": USER_AGENT}
    if key:
        url = PAID_URL
        headers |= {"user_key": key, "key_type": "3scale"}
    timeout = config.getfloat("books", "timeout", fallback=DEFAULT_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.get(url, params={"upc": upc})
    except httpx.HTTPError as error:
        # The key travels in a header, never in the URL or the message.
        logger.warning("UPCitemdb lookup of %s failed: %s", upc, error)
        raise UpcServiceUnavailable("UPC lookup service unreachable.") from error

    if response.status_code in (400, 404):
        # INVALID_UPC or not found: a definite miss.
        return None
    if response.status_code != 200:
        logger.warning(
            "UPCitemdb lookup of %s failed: HTTP %s", upc, response.status_code
        )
        raise UpcServiceUnavailable(
            "UPC lookup service rate-limited."
            if response.status_code == 429
            else "UPC lookup service failed."
        )
    try:
        payload = response.json()
    except ValueError as error:
        logger.warning("UPCitemdb lookup of %s returned bad JSON", upc)
        raise UpcServiceUnavailable("UPC lookup service failed.") from error
    if not isinstance(payload, dict) or payload.get("code") != "OK":
        code = payload.get("code") if isinstance(payload, dict) else None
        logger.warning("UPCitemdb lookup of %s returned code %s", upc, code)
        raise UpcServiceUnavailable("UPC lookup service failed.")
    return first_isbn(payload)
