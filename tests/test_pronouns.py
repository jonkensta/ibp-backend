"""Pronouns an inmate stated in a letter: recorded, shown, and kept off labels.

The value must reach only the inmate view and the packing station: never a
label, the shipping address, search results or the legacy inmate routes, and
never be filled from the provider's ``sex``.
"""

# pylint: disable=redefined-outer-name,unused-argument

import datetime
import logging
import re

import pytest
from ibp import models, providers

NOW = datetime.datetime.now(datetime.timezone.utc)
FRESH = NOW - datetime.timedelta(hours=1)
STALE = NOW - datetime.timedelta(days=100)

INMATE_URL = "/inmates/Texas/12345"
PRONOUNS_URL = f"{INMATE_URL}/pronouns"
CLEAR_URL = f"{PRONOUNS_URL}/clear"
CODE = "TEX-00012345-0"

# Distinctive, so a search of any response body for them is meaningful.
PRONOUNS = "xe/xem"
INITIALS = "QZ"
PRONOUN_FIELDS = {"pronouns", "pronouns_set_by", "pronouns_set_at"}

ADDRESS_URLS = [
    f"/requests/{CODE}/address",
    "/requests/legacy/501/address",
    "/inmates/Texas/12345/requests/0/address",
    "/inmates/by-request/501/address",
]
LEGACY_INMATE_URLS = ["/requests/legacy/501/inmate", "/inmates/by-request/501"]


@pytest.fixture
def seeded(api_db):
    """Seed a unit and a fresh Texas inmate, with a sex, and a filled request."""
    with api_db.begin() as session:
        session.add(
            models.Unit(
                jurisdiction="Texas",
                name="Alpha",
                street1="1 Main St",
                city="Austin",
                state="TX",
                zipcode="78701",
                shipping_method="Box",
            )
        )
        session.add(
            models.Inmate(
                jurisdiction="Texas",
                id=12345,
                first_name="JANE",
                last_name="DOE",
                sex="Female",
                unit_name="Alpha",
                datetime_fetched=FRESH,
                next_request_index=1,
            )
        )
        session.add(
            models.Request(
                inmate_jurisdiction="Texas",
                inmate_id=12345,
                index=0,
                action="Filled",
                request_id=501,
                date_processed=datetime.date(2026, 10, 2),
                date_postmarked=datetime.date(2026, 9, 28),
            )
        )
    return api_db


def set_pronouns(client, pronouns=PRONOUNS, set_by=INITIALS):
    """PUT an inmate's pronouns, asserting success."""
    response = client.put(PRONOUNS_URL, json={"pronouns": pronouns, "set_by": set_by})
    assert response.status_code == 200, response.text
    return response.json()


def stored(db) -> tuple:
    """The inmate's stored (pronouns, set_by, set_at)."""
    with db() as session:
        inmate = session.get(models.Inmate, ("Texas", 12345))
        return inmate.pronouns, inmate.pronouns_set_by, inmate.pronouns_set_at


def assert_absent(response) -> None:
    """The response names neither the pronouns, the initials nor their fields."""
    assert response.status_code == 200, response.text
    for text in [PRONOUNS, INITIALS, *PRONOUN_FIELDS]:
        assert text not in response.text


def _result(**overrides) -> providers.QueryResult:
    values = {
        "id": 12345,
        "jurisdiction": "Texas",
        "first_name": "JANE",
        "last_name": "DOE",
        "sex": "Female",
        "unit": "Alpha",
        "datetime_fetched": NOW,
    }
    values.update(overrides)
    return providers.QueryResult(**values)


@pytest.fixture
def by_id(monkeypatch):
    """Fake provider ID query answering with the inmate; see ``.calls``."""

    async def query(id_, jurisdictions=None, timeout=None):
        query.calls.append(id_)
        return providers.Results(query.responses, [], [])

    query.calls, query.responses = [], [_result()]
    monkeypatch.setattr(providers, "query_by_inmate_id", query)
    return query


@pytest.fixture
def by_name(monkeypatch):
    """Fake provider name query answering with the inmate; see ``.calls``."""

    async def query(first, last, timeout=None):
        query.calls.append((first, last))
        return providers.Results([_result()], [], [])

    query.calls = []
    monkeypatch.setattr(providers, "query_by_name", query)
    return query


@pytest.fixture
def stale(seeded):
    """Make the inmate stale, so lookups and addressing refresh it."""
    with seeded.begin() as session:
        session.get(models.Inmate, ("Texas", 12345)).datetime_fetched = STALE
    return seeded


def fetched(db) -> datetime.datetime:
    """When the inmate was last fetched from its provider."""
    with db() as session:
        value = session.get(models.Inmate, ("Texas", 12345)).datetime_fetched
        return value.replace(tzinfo=datetime.timezone.utc)


# --- Setting and clearing ----------------------------------------------------


def test_set_and_read_back(client, seeded):
    """Set pronouns are returned and shown in the inmate view with who and when."""
    before = datetime.datetime.now(datetime.timezone.utc)
    body = set_pronouns(client)
    assert (body["pronouns"], body["set_by"]) == (PRONOUNS, INITIALS)
    set_at = datetime.datetime.fromisoformat(body["set_at"])
    assert set_at.utcoffset() == datetime.timedelta(0)
    assert before <= set_at <= datetime.datetime.now(datetime.timezone.utc)

    inmate = client.get(INMATE_URL).json()
    assert (inmate["pronouns"], inmate["pronouns_set_by"]) == (PRONOUNS, INITIALS)
    assert datetime.datetime.fromisoformat(inmate["pronouns_set_at"]) == set_at


def test_set_replaces_value(client, seeded):
    """Setting again replaces the value, the initials and the time."""
    first = set_pronouns(client)
    second = set_pronouns(client, pronouns="they/them", set_by="AB")
    assert second["set_at"] >= first["set_at"]
    inmate = client.get(INMATE_URL).json()
    assert (inmate["pronouns"], inmate["pronouns_set_by"]) == ("they/them", "AB")


def test_set_cleans_input(client, seeded):
    """The value is cleaned of stray whitespace; the initials are stripped."""
    body = set_pronouns(client, pronouns="  she / her\t", set_by="  QZ ")
    assert (body["pronouns"], body["set_by"]) == ("she / her", "QZ")


@pytest.mark.parametrize(
    "pronouns", ["he/him", "she/her", "they/them", "Ze/Hir", "any pronouns", "it's"]
)
def test_accepted_values(client, seeded, pronouns):
    """Written-out pronouns in the allowed characters are accepted as is."""
    assert set_pronouns(client, pronouns=pronouns)["pronouns"] == pronouns


def test_clear(client, seeded):
    """Clearing empties all three fields; clearing again is fine."""
    set_pronouns(client)
    for _ in range(2):
        response = client.post(CLEAR_URL, json={"set_by": INITIALS})
        assert response.status_code == 204, response.text
        assert stored(seeded) == (None, None, None)

    inmate = client.get(INMATE_URL).json()
    assert {field: inmate[field] for field in PRONOUN_FIELDS} == dict.fromkeys(
        PRONOUN_FIELDS
    )


@pytest.mark.usefixtures("seeded")
def test_unknown_inmate_is_404(client):
    """Both endpoints are a 404 for an unknown inmate."""
    url = "/inmates/Texas/99999/pronouns"
    body = {"pronouns": PRONOUNS, "set_by": INITIALS}
    assert client.put(url, json=body).status_code == 404
    assert client.post(f"{url}/clear", json={"set_by": INITIALS}).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"pronouns": "", "set_by": INITIALS},
        {"pronouns": "   ", "set_by": INITIALS},
        {"pronouns": "a" * 31, "set_by": INITIALS},
        {"pronouns": "she/her; see letter", "set_by": INITIALS},
        {"pronouns": "<b>he</b>", "set_by": INITIALS},
        {"pronouns": "/him", "set_by": INITIALS},
        {"pronouns": "they/them 2", "set_by": INITIALS},
        {"pronouns": None, "set_by": INITIALS},
        {"set_by": INITIALS},
        {"pronouns": PRONOUNS},
        {"pronouns": PRONOUNS, "set_by": ""},
        {"pronouns": PRONOUNS, "set_by": "   "},
        {"pronouns": PRONOUNS, "set_by": "Q" * 41},
    ],
)
def test_set_rejects_invalid(client, seeded, body):
    """Blank, long, disallowed or missing values or initials are a 422."""
    set_pronouns(client)
    response = client.put(PRONOUNS_URL, json=body)
    assert response.status_code == 422, response.text
    assert stored(seeded)[:2] == (PRONOUNS, INITIALS)


@pytest.mark.parametrize("body", [{}, {"set_by": ""}, {"set_by": "  "}, None])
def test_clear_requires_initials(client, seeded, body):
    """Clearing without initials is a 422 and leaves the value."""
    set_pronouns(client)
    response = client.post(CLEAR_URL, json=body)
    assert response.status_code == 422, response.text
    assert stored(seeded)[:2] == (PRONOUNS, INITIALS)


def test_value_and_initials_are_never_logged(client, seeded, caplog, monkeypatch):
    """Neither the pronouns nor the initials reach the log, at any level."""
    caplog.set_level(logging.DEBUG)
    # Alembic's fileConfig (run by the migration tests) disables loggers
    # that already exist, ibp.api among them.
    monkeypatch.setattr(logging.getLogger("ibp.api"), "disabled", False)
    set_pronouns(client)
    client.post(CLEAR_URL, json={"set_by": INITIALS})
    # The database drivers echo statement parameters at DEBUG; production
    # quiets them to errors (base.configure_external_loggers).
    drivers = ("aiosqlite", "sqlalchemy.engine")
    messages = [
        record.getMessage()
        for record in caplog.records
        if not record.name.startswith(drivers)
    ]
    assert any("pronouns" in message for message in messages)
    for message in messages:
        assert PRONOUNS not in message
        assert INITIALS not in message


# --- Never inferred ----------------------------------------------------------


def test_new_inmate_has_no_pronouns_whatever_its_sex(client, api_db, by_id):
    """An inmate first stored from a provider has none, though it has a sex."""
    by_id.responses = [_result(id=67890, sex="Male")]
    assert client.get("/inmates?query=67890").status_code == 200

    inmate = client.get("/inmates/Texas/67890").json()
    assert inmate["sex"] == "Male"
    assert {field: inmate[field] for field in PRONOUN_FIELDS} == dict.fromkeys(
        PRONOUN_FIELDS
    )


def test_seeded_inmate_has_no_pronouns(client, seeded):
    """An inmate with a sex but nothing recorded shows no pronouns."""
    inmate = client.get(INMATE_URL).json()
    assert inmate["sex"] == "Female"
    assert inmate["pronouns"] is None


# --- Kept through provider upserts -------------------------------------------


def test_survives_id_search(client, seeded, by_id):
    """An ID search re-upserts the inmate and keeps the pronouns."""
    set_pronouns(client)
    expected = stored(seeded)
    assert client.get("/inmates?query=12345").status_code == 200
    assert by_id.calls == [12345]
    assert fetched(seeded) == NOW
    assert stored(seeded) == expected


def test_survives_name_search(client, seeded, by_name):
    """A name search re-upserts the inmate and keeps the pronouns."""
    set_pronouns(client)
    expected = stored(seeded)
    assert client.get("/inmates?query=jane doe").status_code == 200
    assert by_name.calls
    assert fetched(seeded) == NOW
    assert stored(seeded) == expected


def test_survives_lookup_refresh(client, stale, by_id):
    """Opening a stale inmate refreshes it and keeps the pronouns."""
    set_pronouns(client)
    expected = stored(stale)
    response = client.post(f"{INMATE_URL}/lookups")
    assert response.status_code == 201, response.text
    assert by_id.calls == [12345]
    assert fetched(stale) == NOW
    assert stored(stale) == expected
    assert response.json()["pronouns"] == PRONOUNS


def test_survives_address_refresh(client, stale, by_id):
    """Addressing a stale inmate's request refreshes it and keeps the pronouns."""
    set_pronouns(client)
    expected = stored(stale)
    response = client.get(f"/requests/{CODE}/address")
    assert response.status_code == 200, response.text
    assert response.json()["inmate_status"] == "current"
    assert by_id.calls == [12345]
    assert fetched(stale) == NOW
    assert stored(stale) == expected


# --- Kept off everything else ------------------------------------------------


@pytest.mark.parametrize("query", ["12345", "jane doe"])
def test_absent_from_search_results(client, seeded, query, monkeypatch):
    """Search results never carry the pronouns."""
    set_pronouns(client)

    async def no_answer(*_args, **_kwargs):
        return providers.Results([], [], [])

    monkeypatch.setattr(providers, "query_by_inmate_id", no_answer)
    monkeypatch.setattr(providers, "query_by_name", no_answer)

    response = client.get(f"/inmates?query={query}")
    assert [inmate["id"] for inmate in response.json()["inmates"]] == [12345]
    assert_absent(response)


@pytest.mark.parametrize("url", ADDRESS_URLS)
def test_absent_from_shipping_address(client, seeded, url):
    """The shipping address, including its name line, is unchanged by pronouns."""
    before = client.get(url)
    assert before.status_code == 200, before.text
    set_pronouns(client)
    after = client.get(url)
    assert_absent(after)
    assert after.json() == before.json()
    assert after.json()["name"] == "Jane Doe #00012345"


@pytest.mark.parametrize("url", LEGACY_INMATE_URLS)
def test_absent_from_legacy_inmate_routes(client, seeded, url):
    """The legacy inmate routes the Book Portal extension calls leave them out."""
    set_pronouns(client)
    response = client.get(url)
    assert response.json()["id"] == 12345
    assert_absent(response)


def test_label_png_is_unchanged(client, seeded):
    """The label PNG is byte-identical before and after pronouns are set."""
    before = client.get(f"/requests/{CODE}/label")
    assert before.status_code == 200
    set_pronouns(client)
    after = client.get(f"/requests/{CODE}/label")
    assert after.status_code == 200
    assert after.content == before.content


def without_pdf_dates(pdf: bytes) -> bytes:
    """A PDF with its creation and modification times (Pillow's) masked."""
    return re.sub(rb"/(CreationDate|ModDate) \(D:[^)]*\)", rb"/\1 ()", pdf)


def test_label_pdf_is_unchanged(client, seeded):
    """The label PDF is identical before and after, but for its timestamps."""
    before = client.get(f"/requests/{CODE}/label.pdf")
    assert before.status_code == 200
    assert without_pdf_dates(before.content) != before.content
    set_pronouns(client)
    after = client.get(f"/requests/{CODE}/label.pdf")
    assert after.status_code == 200
    assert without_pdf_dates(after.content) == without_pdf_dates(before.content)
