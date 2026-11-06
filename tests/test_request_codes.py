"""Parsing and formatting request codes (the label text, e.g. TEX-1234567-2)."""

import pytest

from ibp import models
from ibp.labels import RequestKey, label_package_id, parse_request_code


@pytest.mark.parametrize(
    "code, expected",
    [
        ("TEX-1234567-2", RequestKey("Texas", 1234567, 2)),
        ("tex-1234567-2", RequestKey("Texas", 1234567, 2)),
        ("Tex-01234567-2", RequestKey("Texas", 1234567, 2)),
        ("TEX-00012345-0", RequestKey("Texas", 12345, 0)),
        ("FED-12345678-10", RequestKey("Federal", 12345678, 10)),
        ("fed-7654321-0", RequestKey("Federal", 7654321, 0)),
        (" TEX-12345-1 ", RequestKey("Texas", 12345, 1)),
    ],
)
def test_parse_request_code(code, expected):
    """Codes are case-insensitive and the inmate number may be zero-padded."""
    assert parse_request_code(code) == expected


@pytest.mark.parametrize(
    "code",
    [
        "",
        "TEX",
        "TEX-12345",
        "TEX-12345-",
        "TEX--12345-0",
        "TEX-12345-0-1",
        "TEX-abc-0",
        "TEX-12345-x",
        "TX-12345-0",
        "USA-12345-0",
        "TEX_12345_0",
        "TEX-12345--1",
        "TEX-1234567890123-0",
        "TEX-12345-1234567890",
        "12345",
    ],
)
def test_parse_request_code_malformed(code):
    """Anything that is not a request code parses to None."""
    assert parse_request_code(code) is None


@pytest.mark.parametrize(
    "jurisdiction, inmate_id, index, code",
    [("Texas", 12345, 0, "TEX-12345-0"), ("Federal", 7654321, 3, "FED-7654321-3")],
)
def test_format_round_trips(jurisdiction, inmate_id, index, code):
    """label_package_id formats a code that parses back to the same key."""
    request = models.Request(
        inmate_jurisdiction=jurisdiction, inmate_id=inmate_id, index=index
    )
    assert label_package_id(request) == code
    assert parse_request_code(code) == RequestKey(jurisdiction, inmate_id, index)
