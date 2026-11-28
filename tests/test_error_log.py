"""backend.jsonl: the backend's own WARNING and above in the shared format."""

# pylint: disable=redefined-outer-name

import json
import logging
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ibp import base, error_log

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$")
EXTERNAL = ["asyncio", "uvicorn", "sqlalchemy.engine", "aiosqlite", "httpx"]


@pytest.fixture(autouse=True)
def enabled_loggers():
    """Undo an earlier test's ``logging.config.fileConfig`` (alembic's
    env.py), which disables the loggers that existed when it ran."""
    for name in ("ibp.base", "ibp.error_log", "ibp.client_errors"):
        logging.getLogger(name).disabled = False


def record(level=logging.ERROR, msg="failed %s", args=("x",), **extra):
    """A log record for ``ibp.test``."""
    item = logging.LogRecord("ibp.test", level, __file__, 1, msg, args, None)
    item.__dict__.update(extra)
    return item


def test_formatter_writes_the_shared_fields(monkeypatch):
    """ts with its UTC offset, level, app, version, host, logger, msg."""
    monkeypatch.setenv("IBP_VERSION", "v1.0.22")
    monkeypatch.setenv("IBP_LOG_HOST", "ibp-server")
    event = json.loads(error_log.JsonFormatter().format(record()))
    assert TS.match(event["ts"])
    assert event["level"] == "ERROR"
    assert event["app"] == "backend"
    assert event["version"] == "v1.0.22"
    assert event["host"] == "ibp-server"
    assert event["logger"] == "ibp.test"
    assert event["msg"] == "failed x"
    assert "exc" not in event and "data" not in event


def test_formatter_adds_exc_and_data():
    """The traceback goes in exc, a record's data dict in data."""
    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()
    item = record(exc_info=exc_info, data={"package": "TEX-00012345-0"})
    event = json.loads(error_log.JsonFormatter().format(item))
    assert event["exc"].startswith("Traceback")
    assert "ValueError: boom" in event["exc"]
    assert event["data"] == {"package": "TEX-00012345-0"}


def test_version_and_host_defaults(monkeypatch):
    """Without the environment the version is dev and the host the machine's."""
    monkeypatch.delenv("IBP_VERSION", raising=False)
    monkeypatch.delenv("IBP_LOG_HOST", raising=False)
    assert error_log.get_version() == "dev"
    assert error_log.get_host()


def test_log_dir_from_environment(tmp_path, monkeypatch):
    """IBP_LOG_DIR sets the directory; the default is logs/ by the config."""
    monkeypatch.setenv("IBP_LOG_DIR", str(tmp_path))
    assert error_log.get_log_dir() == tmp_path
    monkeypatch.delenv("IBP_LOG_DIR")
    expected = (
        base.config.path.parent if base.config.path else base.get_toplevel_path()
    ) / "logs"
    assert error_log.get_log_dir() == expected


def test_backend_handler_unwritable_dir(tmp_path, monkeypatch):
    """A log directory that can't be made gives no handler and a reason."""
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("IBP_LOG_DIR", str(blocker / "logs"))
    handler, problem = error_log.build_backend_handler()
    assert handler is None
    assert problem and "backend.jsonl" in problem


@pytest.fixture
def restore_logging():
    """Undo what the lifespan does to the root and external loggers."""
    root = logging.getLogger()
    saved = root.level, root.handlers[:]
    external = {name: logging.getLogger(name) for name in EXTERNAL}
    saved_external = {
        name: (logger.level, logger.handlers[:]) for name, logger in external.items()
    }
    yield
    for handler in root.handlers:
        if handler not in saved[1]:
            handler.close()
    root.setLevel(saved[0])
    root.handlers[:] = saved[1]
    for name, (level, handlers) in saved_external.items():
        external[name].setLevel(level)
        external[name].handlers[:] = handlers
    error_log.client_error_log.close()


def test_lifespan_writes_warnings_to_backend_jsonl(
    tmp_path: Path, monkeypatch, restore_logging
):  # pylint: disable=unused-argument
    """After startup, WARNING and above (not INFO) land in backend.jsonl."""
    monkeypatch.setenv("IBP_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("IBP_VERSION", "v9.9.9")
    # As uvicorn configures it when it runs the app.
    monkeypatch.setattr(logging.getLogger("uvicorn"), "propagate", False)
    with TestClient(base.app):
        logging.getLogger("ibp.something").info("routine")
        logging.getLogger("ibp.something").warning("odd %d", 3)
        try:
            raise RuntimeError("bad")
        except RuntimeError:
            logging.getLogger("ibp.something").exception("failed")
    lines = (tmp_path / "backend.jsonl").read_text().splitlines()
    found = [json.loads(line) for line in lines]
    assert [(e["level"], e["msg"]) for e in found] == [
        ("WARNING", "odd 3"),
        ("ERROR", "failed"),
    ]
    assert all(e["app"] == "backend" and e["version"] == "v9.9.9" for e in found)
    assert "RuntimeError: bad" in found[1]["exc"]
    # Uvicorn's loggers don't propagate; their errors reach the file too.
    assert any(
        isinstance(h, logging.FileHandler)
        for h in logging.getLogger("uvicorn").handlers
    )


def test_lifespan_starts_without_a_writable_log_dir(
    tmp_path: Path, monkeypatch, restore_logging, capsys
):  # pylint: disable=unused-argument
    """The backend still starts and answers; stderr says why there is no file."""
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("IBP_LOG_DIR", str(blocker / "logs"))
    with TestClient(base.app) as client:
        assert client.get("/openapi.json").status_code == 200
    err = capsys.readouterr().err
    assert "Not writing backend.jsonl" in err
    assert "Not writing client-errors.jsonl" in err
