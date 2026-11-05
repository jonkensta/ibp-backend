"""Config file lookup: CONF, then server.conf, then sample.conf."""

# pylint: disable=redefined-outer-name,unused-argument

from pathlib import Path

import pytest

from ibp import base


@pytest.fixture
def toplevel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Use an empty directory as the project toplevel, with CONF unset."""
    monkeypatch.setattr(base, "get_toplevel_path", lambda: tmp_path)
    monkeypatch.delenv("CONF", raising=False)
    return tmp_path


def _write(path: Path, level: str) -> Path:
    path.write_text(f"[logging]\nlevel = {level}\n", encoding="utf-8")
    return path


def test_conf_absolute_path(
    toplevel: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory
) -> None:
    """An absolute CONF is read as is, ahead of server.conf."""
    _write(toplevel / "server.conf", "WARNING")
    conf = _write(tmp_path_factory.mktemp("config") / "custom.conf", "DEBUG")
    monkeypatch.setenv("CONF", str(conf))

    config = base.read_server_config()
    assert config.get("logging", "level") == "DEBUG"
    assert config.path == conf.resolve()


def test_conf_relative_path(toplevel: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative CONF is resolved against the project toplevel."""
    _write(toplevel / "custom.conf", "DEBUG")
    monkeypatch.setenv("CONF", "custom.conf")

    config = base.read_server_config()
    assert config.path == (toplevel / "custom.conf").resolve()


def test_conf_missing_fails_loudly(
    toplevel: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CONF that does not exist is an error naming the path, not a fallback."""
    _write(toplevel / "sample.conf", "INFO")
    monkeypatch.setenv("CONF", "/nonexistent/server.conf")

    with pytest.raises(FileNotFoundError, match="/nonexistent/server.conf"):
        base.read_server_config()


def test_conf_directory_fails_loudly(
    toplevel: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CONF naming a directory is an error too."""
    (toplevel / "configdir").mkdir()
    monkeypatch.setenv("CONF", "configdir")

    with pytest.raises(FileNotFoundError, match="configdir"):
        base.read_server_config()


def test_server_conf_preferred_over_sample(toplevel: Path) -> None:
    """Without CONF, server.conf wins over sample.conf."""
    _write(toplevel / "server.conf", "WARNING")
    _write(toplevel / "sample.conf", "INFO")

    config = base.read_server_config()
    assert config.get("logging", "level") == "WARNING"
    assert config.path == (toplevel / "server.conf").resolve()


def test_sample_conf_fallback(toplevel: Path) -> None:
    """Without CONF or server.conf, sample.conf is read."""
    _write(toplevel / "sample.conf", "INFO")

    config = base.read_server_config()
    assert config.path == (toplevel / "sample.conf").resolve()


def test_no_config_file(toplevel: Path) -> None:
    """With nothing to read, startup fails."""
    with pytest.raises(FileNotFoundError, match="No configuration file found"):
        base.read_server_config()


def test_repo_sample_conf_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped sample.conf parses (the Docker image relies on it)."""
    monkeypatch.delenv("CONF", raising=False)
    path = base.get_toplevel_path() / "sample.conf"
    config = base.load_config_file(path)
    assert config.get("logging", "level") == "INFO"
