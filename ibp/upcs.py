"""UPC -> ISBN lookup, for books whose barcode is a retail UPC-A.

Some mass-market paperbacks carry a UPC-A (often with a 2- or 5-digit add-on)
instead of the ISBN barcode. UPCitemdb maps many UPCs to an ISBN; the
endpoint then does the normal ISBN lookup (see ``ibp.book_api``).

UPCitemdb's free trial endpoint needs no key but allows 100 requests a day
per public IP; with ``[books] upcitemdb_key`` set, the paid endpoint is used
instead. When UPCitemdb is rate-limited or down and ``[books]
barcodelookup_key`` is set, Barcode Lookup is asked instead (its free plan
allows 50 requests a month, so it is only a fallback).
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
BARCODELOOKUP_URL = "https://api.barcodelookup.com/v3/products"

# UPC-A plus an optional 2- or 5-digit add-on.
CODE_LENGTHS = (12, 14, 17)


class UpcServiceUnavailable(Exception):
    """The UPC service is rate-limited or unreachable; don't cache the answer."""


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


# "ISBN-10 0140449132, ISBN-13 9780140449136" in Barcode Lookup's
# barcode_formats.
_FORMAT_ISBN = re.compile(r"ISBN-1[03]\s+([\dX]{10}(?:\d{3})?)", re.IGNORECASE)


def product_isbn(product: dict[str, Any]) -> Optional[str]:
    """The ISBN-13 of one Barcode Lookup product, from barcode_formats or a
    978/979 barcode_number."""
    for match in _FORMAT_ISBN.finditer(str(product.get("barcode_formats") or "")):
        if isbn := normalize_isbn(match.group(1)):
            return isbn
    number = str(product.get("barcode_number") or "")
    if re.fullmatch(r"97[89]\d{10}", number):
        return normalize_isbn(number)
    return None


async def barcodelookup(upc: str, key: str) -> Optional[str]:
    """Ask Barcode Lookup for the ISBN-13 of a 12-digit UPC; None if it has none.

    Raises UpcServiceUnavailable when rate-limited, failing or unreachable.
    The key goes in the query string (the API has no header for it), so
    neither URLs nor httpx errors (which may quote the URL) are logged.
    """
    timeout = config.getfloat("books", "timeout", fallback=DEFAULT_TIMEOUT)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": USER_AGENT}
        ) as client:
            response = await client.get(
                BARCODELOOKUP_URL, params={"barcode": upc, "key": key}
            )
    except httpx.HTTPError as error:
        logger.warning("Barcode Lookup of %s failed: %s", upc, type(error).__name__)
        raise UpcServiceUnavailable("UPC lookup service unreachable.") from None

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.warning(
            "Barcode Lookup of %s failed: HTTP %s", upc, response.status_code
        )
        raise UpcServiceUnavailable(
            "UPC lookup service rate-limited."
            if response.status_code in (403, 429)
            else "UPC lookup service failed."
        )
    try:
        payload = response.json()
    except ValueError:
        logger.warning("Barcode Lookup of %s returned bad JSON", upc)
        raise UpcServiceUnavailable("UPC lookup service failed.") from None
    products = payload.get("products") if isinstance(payload, dict) else None
    for product in products or []:
        if isinstance(product, dict) and (isbn := product_isbn(product)):
            return isbn
    return None


async def lookup_isbn(upc: str) -> tuple[Optional[str], str]:
    """The ISBN-13 for a 12-digit UPC (None if unknown) and the service's name.

    UPCitemdb first; Barcode Lookup only when UPCitemdb is unavailable and a
    key is set. Raises UpcServiceUnavailable when no service could answer.
    """
    try:
        return await upcitemdb(upc), "upcitemdb"
    except UpcServiceUnavailable:
        key = setting("barcodelookup_key")
        if not key:
            raise
    return await barcodelookup(upc, key), "barcodelookup"
