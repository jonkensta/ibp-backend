"""UPC codes printed on books, for the local UPC -> ISBN map.

Some mass-market paperbacks carry a UPC-A (often with a 2- or 5-digit add-on)
instead of the ISBN barcode. The 12-digit UPC identifies the publisher and
price point, so many titles share it; the add-on tells them apart. The
``upcs`` table maps a full code (UPC plus add-on) to the ISBN a volunteer
scanned for it (see ``ibp.book_api``). No outside service is asked.
"""

from __future__ import annotations

import re

# UPC-A plus an optional 2- or 5-digit add-on.
CODE_LENGTHS = (12, 14, 17)


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
