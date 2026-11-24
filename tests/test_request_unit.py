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
    """Each stored request's unit_name, by index, or ``"not recorded"``."""
    with api_db() as session:
        return [
            request.unit_name if request.unit_recorded else "not recorded"
            for request in session.query(models.Request).order_by(models.Request.index)
        ]


def test_filled_request_records_the_inmates_unit(client, seeded):
    """A new Filled request records the unit; the API doesn't expose it."""
    response = client.post(REQUESTS_URL, json={**BODY, "action": "Filled"})
    assert response.status_code == 200
    assert not {"unit_name", "unit_recorded"} & set(response.json())
    assert stored_units(seeded) == ["ALPHA"]


def test_tossed_request_records_no_unit(client, seeded):
    """A new Tossed request has no unit."""
    client.post(REQUESTS_URL, json={**BODY, "action": "Tossed"})
    assert stored_units(seeded) == ["not recorded"]


def test_inmate_without_a_unit_records_none(client, seeded):
    """An inmate with no unit gives a Filled request none, recorded as such."""
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


def request(action=None, unit_name=None, recorded=False) -> models.Request:
    """An unsaved request with ``action``, ``unit_name`` and
    ``unit_recorded``."""
    date = datetime.date(2026, 10, 2)
    return models.Request(
        inmate_jurisdiction="Texas",
        inmate_id=12345,
        index=0,
        date_processed=date,
        date_postmarked=date,
        action=action,
        unit_name=unit_name,
        unit_recorded=recorded,
    )


@pytest.mark.parametrize(
    "before, recorded, action, current, after",
    [
        # Becoming Filled records the current unit, even an unknown one.
        (None, (None, False), "Filled", "BETA", ("BETA", True)),
        ("Tossed", (None, False), "Filled", "BETA", ("BETA", True)),
        (None, (None, False), "Filled", None, (None, True)),
        # Already Filled: what was recorded stays, or that nothing was.
        ("Filled", ("ALPHA", True), "Filled", "BETA", ("ALPHA", True)),
        ("Filled", (None, True), "Filled", "BETA", (None, True)),
        ("Filled", (None, False), "Filled", "BETA", (None, False)),
        # Tossed has no unit.
        ("Filled", ("ALPHA", True), "Tossed", "BETA", (None, False)),
        (None, (None, False), "Tossed", "BETA", (None, False)),
    ],
)
def test_set_action(before, recorded, action, current, after):
    """``set_action`` records, keeps or clears the unit."""
    changed = request(before, *recorded)
    changed.set_action(action, current)
    assert changed.action == action
    assert (changed.unit_name, changed.unit_recorded) == after
