"""Tests for scripts/shipment-sync/ibp-shipment-sync (no docker is run)."""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "shipment-sync"
    / "ibp-shipment-sync"
)
_loader = importlib.machinery.SourceFileLoader("ibp_shipment_sync", str(SCRIPT))
_spec = importlib.util.spec_from_loader("ibp_shipment_sync", _loader)
assert _spec is not None
wrapper = importlib.util.module_from_spec(_spec)
_loader.exec_module(wrapper)


def test_key_is_mounted_read_only_not_passed():
    """The key file is mounted into the one-off container; its path, not the
    key, is on the command line."""
    command = wrapper.build_command(Path("/home/x/key"), ["--since", "2025-11-01"])
    assert command[:4] == ["docker", "compose", "-f", wrapper.COMPOSE]
    assert "run" in command and "--rm" in command
    assert f"/home/x/key:{wrapper.KEY_IN_CONTAINER}:ro" in command
    assert command[-5:] == [
        "ibp.shipment_sync",
        "--key-file",
        wrapper.KEY_IN_CONTAINER,
        "--since",
        "2025-11-01",
    ]


def test_rebuild_links_mounts_no_key():
    """--rebuild-links runs without the key file."""
    command = wrapper.build_command(Path("/home/x/key"), ["--rebuild-links"])
    assert "-v" not in command and "--key-file" not in command
    assert command[-1] == "--rebuild-links"


def test_missing_key_file_fails_clearly(tmp_path, monkeypatch, capsys):
    """Without the key file nothing is run and the message says where it goes."""
    monkeypatch.setattr(wrapper, "CONFIG", tmp_path)

    def no_docker(*_args, **_kwargs):
        raise AssertionError("docker must not run")

    monkeypatch.setattr(wrapper.subprocess, "Popen", no_docker)
    log: list[str] = []
    assert wrapper.run([], log) == 2
    assert f"create {tmp_path / 'easypost-api-key'}" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["--since", "2025-11-01"]])
def test_exit_status_is_passed_through(tmp_path, monkeypatch, args):
    """The container's exit status and output are the wrapper's."""
    (tmp_path / "easypost-api-key").write_text("EZAKtest\n")
    monkeypatch.setattr(wrapper, "CONFIG", tmp_path)
    seen = {}

    class FakeProcess:  # pylint: disable=too-few-public-methods
        """Stands in for the docker compose process."""

        returncode = 1
        stdout = iter(["shipment sync: 0 labels seen\n"])

        def __init__(self, command, **_kwargs):
            seen["command"] = command

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(wrapper.subprocess, "Popen", FakeProcess)
    log: list[str] = []
    assert wrapper.run(args, log) == 1
    assert log == ["shipment sync: 0 labels seen"]
    command = seen["command"]
    assert command[len(command) - len(args) :] == args
    assert "EZAKtest" not in command
