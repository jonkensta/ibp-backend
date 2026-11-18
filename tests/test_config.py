"""Config file lookup: CONF or server.conf, read over sample.conf."""

# pylint: disable=redefined-outer-name,unused-argument

import shutil
from pathlib import Path

import pytest

from ibp import base

REPO_SAMPLE_CONF = base.get_toplevel_path() / "sample.conf"


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


def test_partial_server_conf_falls_back_to_sample(
    toplevel: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server.conf without [database] starts on the sample's sections."""
    shutil.copy(REPO_SAMPLE_CONF, toplevel / "sample.conf")
    _write(toplevel / "server.conf", "WARNING")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    config = base.read_server_config()
    assert config.get("logging", "level") == "WARNING"
    assert config.get("database", "uri") == "sqlite+aiosqlite:///data.db"
    assert config.getint("warnings", "inmates_cache_ttl") == 24
    assert config.get("logging", "backup_count") == "5"
    assert config.path == (toplevel / "server.conf").resolve()


def test_server_conf_overrides_sample_options(toplevel: Path) -> None:
    """Within a section, server.conf's options win and the rest are inherited."""
    (toplevel / "sample.conf").write_text(
        "[warnings]\nmin_release_timedelta = 60\nmin_postmark_timedelta = 90\n",
        encoding="utf-8",
    )
    (toplevel / "server.conf").write_text(
        "[warnings]\nmin_release_timedelta = 30\n", encoding="utf-8"
    )

    config = base.read_server_config()
    assert config.getint("warnings", "min_release_timedelta") == 30
    assert config.getint("warnings", "min_postmark_timedelta") == 90


def test_conf_overrides_sample(toplevel: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONF is read over sample.conf too, and ahead of server.conf."""
    (toplevel / "sample.conf").write_text(
        "[logging]\nlevel = INFO\n[providers]\ntimeout = 5.0\n", encoding="utf-8"
    )
    _write(toplevel / "server.conf", "WARNING")
    _write(toplevel / "custom.conf", "DEBUG")
    monkeypatch.setenv("CONF", "custom.conf")

    config = base.read_server_config()
    assert config.get("logging", "level") == "DEBUG"
    assert config.getfloat("providers", "timeout") == 5.0
    assert config.path == (toplevel / "custom.conf").resolve()


def test_server_conf_without_sample(toplevel: Path) -> None:
    """With no sample.conf, server.conf is read alone."""
    _write(toplevel / "server.conf", "WARNING")

    config = base.read_server_config()
    assert config.get("logging", "level") == "WARNING"
    assert config.sections() == ["logging"]


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
    config = base.load_config_files([path])
    assert config.get("logging", "level") == "INFO"
