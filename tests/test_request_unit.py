"""The unit a request records when it is filled (``requests.unit_name``)."""

# pylint: disable=redefined-outer-name,unused-argument

import datetime

import pytest

from ibp import models

from .shipment_helpers import add_inmate, add_unit

REQUESTS_URL = "/inmates/Texas/12345/requests"
BODY = {"date_processed": "2026-10-02", "date_postmarked": "2026-09-28"}


@pytest.fixture
def seeded(api_db):
    """One Texas inmate at invented unit ALPHA (and unit BETA elsewhere)."""
    with api_db.begin() as session:
        add_unit(session, "ALPHA")
        add_unit(session, "BETA", street1="200 Other Way")
        add_inmate(session, 12345, "ALPHA")
    return api_db


def stored_units(api_db) -> list:
    """Each stored request's unit_name, by index."""
    with api_db() as session:
        return [
            request.unit_name
            for request in session.query(models.Request).order_by(models.Request.index)
        ]


def test_filled_request_records_the_inmates_unit(client, seeded):
    """A new Filled request records the unit; the API doesn't expose it."""
    response = client.post(REQUESTS_URL, json={**BODY, "action": "Filled"})
    assert response.status_code == 200
    assert "unit_name" not in response.json()
    assert stored_units(seeded) == ["ALPHA"]


def test_tossed_request_records_no_unit(client, seeded):
    """A new Tossed request has no unit."""
    client.post(REQUESTS_URL, json={**BODY, "action": "Tossed"})
    assert stored_units(seeded) == [None]


def test_inmate_without_a_unit_records_none(client, seeded):
    """An inmate with no unit gives a Filled request none."""
    with seeded.begin() as session:
        add_inmate(session, 54321)
    client.post("/inmates/Texas/54321/requests", json={**BODY, "action": "Filled"})
    assert stored_units(seeded) == [None]


def test_transfer_keeps_the_recorded_unit(client, seeded):
    """A transfer after the fill leaves the recorded unit alone; a request
    filled after it records the new unit."""
    client.post(REQUESTS_URL, json={**BODY, "action": "Filled"})
    with seeded.begin() as session:
        session.get(models.Inmate, ("Texas", 12345)).unit_name = "BETA"
    client.post(REQUESTS_URL, json={**BODY, "action": "Filled"})
    assert stored_units(seeded) == ["ALPHA", "BETA"]


def request(action=None, unit_name=None) -> models.Request:
    """An unsaved request with ``action`` and ``unit_name``."""
    date = datetime.date(2026, 10, 2)
    return models.Request(
        inmate_jurisdiction="Texas",
        inmate_id=12345,
        index=0,
        date_processed=date,
        date_postmarked=date,
        action=action,
        unit_name=unit_name,
    )


@pytest.mark.parametrize(
    "before, recorded, action, after",
    [
        # Becoming Filled records the current unit.
        (None, None, "Filled", "BETA"),
        ("Tossed", None, "Filled", "BETA"),
        # Already Filled: the recorded unit stays, even if none was recorded.
        ("Filled", "ALPHA", "Filled", "ALPHA"),
        ("Filled", None, "Filled", None),
        # Tossed has no unit.
        ("Filled", "ALPHA", "Tossed", None),
        (None, None, "Tossed", None),
    ],
)
def test_set_action(before, recorded, action, after):
    """``set_action`` records, keeps or clears the unit (current unit BETA)."""
    changed = request(before, recorded)
    changed.set_action(action, "BETA")
    assert (changed.action, changed.unit_name) == (action, after)
