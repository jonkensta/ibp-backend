"""POST /client-errors: browser error reports appended to client-errors.jsonl."""

# pylint: disable=redefined-outer-name

import json
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ibp import client_errors
from ibp.base import app
from ibp.error_log import CLIENT_LOG_NAME, client_error_log

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$")


@pytest.fixture(autouse=True)
def enabled_loggers():
    """Undo an earlier test's ``logging.config.fileConfig`` (alembic's
    env.py), which disables the loggers that existed when it ran."""
    for name in ("ibp.base", "ibp.error_log", "ibp.client_errors"):
        logging.getLogger(name).disabled = False


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh log directory, and the file closed before and after."""
    directory = tmp_path / "logs"
    monkeypatch.setenv("IBP_LOG_DIR", str(directory))
    client_error_log.close()
    yield directory
    client_error_log.close()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A fresh rate limiter on a fake clock."""
    fake = FakeClock()
    monkeypatch.setattr(
        client_errors, "rate_limiter", client_errors.RateLimiter(clock=fake)
    )
    return fake


@pytest.fixture
def client(log_dir, clock) -> TestClient:  # pylint: disable=unused-argument
    """Client without the lifespan; the file opens on the first report."""
    return TestClient(app)


def report(**fields) -> dict:
    """A valid report with ``fields`` changed."""
    body = {
        "level": "ERROR",
        "app": "frontend",
        "version": "v1.0.22",
        "logger": "window.onerror",
        "msg": "TypeError: x is undefined",
        "exc": "TypeError: x is undefined\n    at f (index.js:1:2)",
        "data": {"url": "/packages"},
    }
    body.update(fields)
    return {key: value for key, value in body.items() if value is not None}


def events(log_dir: Path) -> list[dict]:
    """The events written so far."""
    path = log_dir / CLIENT_LOG_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def drops(caplog) -> list[logging.LogRecord]:
    """The rate limiter's WARNINGs captured so far."""
    return [r for r in caplog.records if r.name == "ibp.client_errors"]


def test_report_is_appended_with_ts_and_host(client, log_dir):
    """The server adds ts (with its UTC offset) and host (X-Real-IP)."""
    response = client.post(
        "/client-errors", json=report(), headers={"X-Real-IP": "100.65.137.125"}
    )
    assert response.status_code == 204
    assert response.content == b""
    (event,) = events(log_dir)
    assert TS.match(event.pop("ts"))
    assert event == {
        "level": "ERROR",
        "app": "frontend",
        "version": "v1.0.22",
        "logger": "window.onerror",
        "host": "100.65.137.125",
        "msg": "TypeError: x is undefined",
        "exc": "TypeError: x is undefined\n    at f (index.js:1:2)",
        "data": {"url": "/packages"},
    }


def test_client_ts_host_and_unknown_fields_are_ignored(client, log_dir):
    """Only the server's ts and host are written; unknown fields are dropped."""
    body = report(ts="1999-01-01T00:00:00Z", host="spoofed", extra="x")
    response = client.post(
        "/client-errors", json=body, headers={"X-Real-IP": "192.168.1.26"}
    )
    assert response.status_code == 204
    (event,) = events(log_dir)
    assert event["host"] == "192.168.1.26"
    assert not event["ts"].startswith("1999")
    assert "extra" not in event


def test_optional_fields_left_out(client, log_dir):
    """A minimal report writes only what it has, level upper-cased."""
    body = {"level": "warning", "app": "book-portal-extension", "msg": "odd"}
    assert client.post("/client-errors", json=body).status_code == 204
    (event,) = events(log_dir)
    assert set(event) == {"ts", "level", "app", "host", "msg"}
    assert event["level"] == "WARNING"
    assert event["app"] == "book-portal-extension"


def test_host_without_x_real_ip_is_the_connection(client, log_dir):
    """Without nginx's header (or with a bad one) the peer address is used."""
    client.post("/client-errors", json=report())
    client.post("/client-errors", json=report(), headers={"X-Real-IP": "not an ip"})
    assert [event["host"] for event in events(log_dir)] == ["testclient"] * 2


@pytest.mark.parametrize(
    "body",
    [
        report(app="backend"),
        report(app="shippy"),
        report(level="LOUD"),
        report(msg=None),
        report(data=["not", "an", "object"]),
        ["not an object"],
    ],
)
def test_invalid_report_is_422(client, log_dir, body):
    """An app other than the browser ones, or a malformed event, is refused."""
    assert client.post("/client-errors", json=body).status_code == 422
    assert not events(log_dir)


def test_invalid_json_is_422(client, log_dir):
    """A body that isn't JSON is refused."""
    response = client.post(
        "/client-errors",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert not events(log_dir)


def test_over_16_kib_is_413(client, log_dir):
    """The body may be 16 KiB; one byte more is a 413."""
    base = json.dumps(report(exc=""))
    padding = client_errors.MAX_BODY_BYTES - len(base.encode())
    at_limit = json.dumps(report(exc="x" * padding))
    assert len(at_limit.encode()) == client_errors.MAX_BODY_BYTES
    headers = {"Content-Type": "application/json"}
    response = client.post("/client-errors", content=at_limit, headers=headers)
    assert response.status_code == 204

    over = json.dumps(report(exc="x" * (padding + 1)))
    response = client.post("/client-errors", content=over, headers=headers)
    assert response.status_code == 413
    assert len(events(log_dir)) == 1


def test_over_16_kib_without_content_length_is_413(client, log_dir):
    """A chunked body (no Content-Length) is cut off at the limit too."""

    def chunks():
        yield b'{"level": "ERROR", "app": "frontend", "msg": "'
        for _ in range(20):
            yield b"x" * 1024
        yield b'"}'

    response = client.post(
        "/client-errors",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert not events(log_dir)


def test_rate_limit_per_address(client, log_dir, clock, caplog):
    """30 a minute per address; the rest are dropped, then one WARNING says
    how many once the minute is over."""
    caplog.set_level(logging.WARNING, logger="ibp.client_errors")
    one = {"X-Real-IP": "100.120.251.0"}
    other = {"X-Real-IP": "100.65.137.125"}
    statuses = [
        client.post("/client-errors", json=report(), headers=one).status_code
        for _ in range(33)
    ]
    assert statuses == [204] * 30 + [429] * 3
    # Another address has its own allowance.
    assert (
        client.post("/client-errors", json=report(), headers=other).status_code == 204
    )
    assert len(events(log_dir)) == 31
    assert not drops(caplog)

    clock.now += client_errors.RATE_WINDOW_S
    assert client.post("/client-errors", json=report(), headers=one).status_code == 204
    (record,) = drops(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "dropped 3 client error reports from 100.120.251.0 over the limit of "
        "30 a minute"
    )
    assert len(events(log_dir)) == 32


def test_rate_limiter_reports_each_window_once(caplog):
    """A window's drops are reported once, and a window without drops not at
    all; ended windows are forgotten."""
    caplog.set_level(logging.WARNING, logger="ibp.client_errors")
    clock = FakeClock()
    limiter = client_errors.RateLimiter(limit=2, window_s=60, clock=clock)
    assert [limiter.allow("a") for _ in range(4)] == [True, True, False, False]
    assert limiter.allow("b")
    clock.now += 60
    limiter.flush()
    limiter.flush()
    assert [record.getMessage() for record in drops(caplog)] == [
        "dropped 2 client error reports from a over the limit of 2 a minute"
    ]
    assert not limiter._windows  # pylint: disable=protected-access


def test_unwritable_log_dir_falls_back_to_stderr(
    tmp_path, monkeypatch, clock, capsys
):  # pylint: disable=unused-argument
    """If the file can't be opened the report still succeeds, on stderr."""
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("IBP_LOG_DIR", str(blocker / "logs"))
    client_error_log.close()
    try:
        response = TestClient(app).post("/client-errors", json=report(msg="kept"))
    finally:
        client_error_log.close()
    assert response.status_code == 204
    err = capsys.readouterr().err
    assert '"msg": "kept"' in err
