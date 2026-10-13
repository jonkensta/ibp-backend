"""Logging configuration: INFO by default, bounded rotation, stderr fallback."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from ibp import base


def _use_config(
    monkeypatch: pytest.MonkeyPatch, text: str, path: Path | None = None
) -> None:
    parser = base.EnvInterpolatingConfigParser()
    parser.read_string(text)
    parser.path = path
    monkeypatch.setattr(base, "config", parser)


def _handler(handlers) -> logging.Handler:
    (handler,) = list(handlers)
    return handler


def test_sample_conf_level_is_info() -> None:
    """The shipped template logs at INFO."""
    parser = base.EnvInterpolatingConfigParser()
    parser.read(base.get_toplevel_path() / "sample.conf")
    assert parser.get("logging", "level") == "INFO"


def test_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a level key the root logger is set to INFO."""
    _use_config(monkeypatch, "[logging]\n")
    assert base.get_log_level() == "INFO"

    root = logging.getLogger()
    saved_level, saved_handlers = root.level, root.handlers[:]
    try:
        base.configure_root_logger([])
        assert root.level == logging.INFO
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_no_logfile_logs_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty or missing logfile yields a plain stderr stream handler."""
    for text in ("[logging]\n", "[logging]\nlogfile =\n"):
        _use_config(monkeypatch, text)
        handler = _handler(base.build_log_handlers())
        assert isinstance(handler, logging.StreamHandler)
        assert not isinstance(handler, logging.FileHandler)


def test_logfile_rotation_is_bounded_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defaults: 10 MiB per file and 5 backups."""
    _use_config(monkeypatch, f"[logging]\nlogfile = {tmp_path / 'ibp.log'}\n")
    handler = _handler(base.build_log_handlers())
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 5
    finally:
        handler.close()


def test_logfile_rotation_configurable_with_legacy_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``rotation_size`` is honoured when ``max_bytes`` is absent."""
    text = f"[logging]\nlogfile = {tmp_path / 'ibp.log'}\nrotation_size = 1234\n"
    _use_config(monkeypatch, text + "backup_count = 2\n")
    handler = _handler(base.build_log_handlers())
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert (handler.maxBytes, handler.backupCount) == (1234, 2)
    finally:
        handler.close()

    _use_config(monkeypatch, text + "max_bytes = 4321\n")
    handler = _handler(base.build_log_handlers())
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 4321
    finally:
        handler.close()


@pytest.mark.parametrize("key", ["max_bytes", "backup_count"])
def test_unbounded_rotation_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str
) -> None:
    """Zero disables rotation or backups and would let the log grow unbounded."""
    _use_config(monkeypatch, f"[logging]\nlogfile = {tmp_path / 'x.log'}\n{key} = 0\n")
    with pytest.raises(ValueError):
        list(base.build_log_handlers())


def test_relative_logfile_resolved_against_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative logfile does not depend on the working directory."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    _use_config(
        monkeypatch, "[logging]\nlogfile = logs/ibp.log\n", conf_dir / "server.conf"
    )
    assert base.get_logfile_path() == conf_dir / "logs" / "ibp.log"
