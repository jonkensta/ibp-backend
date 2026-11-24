"""Tests for scripts/shipment-sync/ibp-shipment-sync (no docker is run)."""

import importlib.machinery
import importlib.util
import subprocess
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


def test_runs_are_mutually_exclusive(tmp_path, monkeypatch, capsys):
    """While one run holds the lock, another exits 75 at once, runs nothing
    and pings nothing; once it is released, a run goes ahead."""
    monkeypatch.setattr(wrapper, "LOCK_FILE", tmp_path / "state" / "run.lock")
    monkeypatch.setattr(wrapper, "CONFIG", tmp_path)
    (tmp_path / "ping-url").write_text("https://hc.example/ping\n")
    pings: list[str] = []
    runs: list[list[str]] = []
    monkeypatch.setattr(
        wrapper, "ping", lambda url, suffix, body="": pings.append(suffix)
    )

    def fake_run(args, log):
        runs.append(args)
        log.append("shipment sync: ok")
        return 0

    monkeypatch.setattr(wrapper, "run", fake_run)
    monkeypatch.setattr(
        wrapper.sys, "argv", ["ibp-shipment-sync", "--since", "2025-11-01"]
    )

    held = wrapper.take_lock(wrapper.LOCK_FILE)
    assert held is not None
    try:
        assert wrapper.main() == wrapper.BUSY == 75
        assert "another ibp-shipment-sync run is in progress" in capsys.readouterr().err
        assert not runs and not pings
    finally:
        held.close()

    assert wrapper.main() == 0
    assert runs == [["--since", "2025-11-01"]]
    assert pings == ["/start", "/0"]


def test_lock_is_held_for_the_whole_run(tmp_path, monkeypatch):
    """During a run the lock can't be taken by anyone else."""
    monkeypatch.setattr(wrapper, "LOCK_FILE", tmp_path / "run.lock")
    monkeypatch.setattr(wrapper, "CONFIG", tmp_path)
    monkeypatch.setattr(wrapper, "ping", lambda *args, **kwargs: None)
    monkeypatch.setattr(wrapper.sys, "argv", ["ibp-shipment-sync"])
    during = []

    def fake_run(_args, _log):
        during.append(wrapper.take_lock(wrapper.LOCK_FILE))
        return 0

    monkeypatch.setattr(wrapper, "run", fake_run)
    assert wrapper.main() == 0
    assert during == [None]
    after = wrapper.take_lock(wrapper.LOCK_FILE)
    assert after is not None
    after.close()


def test_service_treats_a_busy_run_as_success():
    """The timer's service doesn't fail when a manual run holds the lock."""
    unit = (SCRIPT.parent / "ibp-shipment-sync.service").read_text()
    assert "SuccessExitStatus=75" in unit


def test_one_off_container_is_labelled():
    """The sync's container carries a label a runbook can wait on."""
    command = wrapper.build_command(Path("/home/x/key"), [])
    assert command[command.index("--label") + 1] == "ibp.job=shipment-sync"
    assert command.index("--label") < command.index("ibp-backend")


@pytest.mark.parametrize(
    "readme", ["deploy/pi/README.md", "scripts/shipment-sync/README.md"]
)
def test_runbook_waits_for_activating_runs_and_installs_the_wrapper_first(readme):
    """The upgrade runbooks wait for a run that is still activating and for
    any one-off sync container, and install the new wrapper and service
    before the backfill, which comes before starting the timer."""
    text = (Path(__file__).resolve().parent.parent / readme).read_text()
    steps = [
        "sudo systemctl stop ibp-shipment-sync.timer",
        'ActiveState --value ibp-shipment-sync.service)" = activating',
        "--filter label=com.docker.compose.oneoff=True",
        "install -D -m755 ibp-shipment-sync ~/.local/bin/ibp-shipment-sync",
        "sudo systemctl daemon-reload",
        "--since 2025-11-01",
        "sudo systemctl start ibp-shipment-sync.timer",
    ]
    positions = [
        text.find(step, text.find("sudo systemctl stop ibp-shipment-sync.timer"))
        for step in steps
    ]
    assert -1 not in positions, dict(zip(steps, positions))
    assert positions == sorted(positions)


RUNBOOKS = ["deploy/pi/README.md", "scripts/shipment-sync/README.md"]
TOPLEVEL = Path(__file__).resolve().parent.parent


def code_blocks(readme: str) -> list[str]:
    """The indented code blocks of a Markdown file, dedented."""
    blocks, current = [], []
    for line in (TOPLEVEL / readme).read_text().splitlines():
        if line.startswith("    "):
            current.append(line[4:])
        elif line.strip() and current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def backfill_steps(readme: str) -> str:
    """Steps 6 and 7 of the re-evaluation block."""
    (block,) = [b for b in code_blocks(readme) if "# 6. Re-evaluate" in b]
    return block[block.index("# 6. Re-evaluate") :]


@pytest.mark.parametrize("readme", RUNBOOKS)
def test_runbook_code_is_valid_shell(readme):
    """Every code block in the runbooks parses as shell."""
    for block in code_blocks(readme):
        script = block.replace("<", "_").replace(">", "_")
        result = subprocess.run(
            ["bash", "-n"], input=script, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, (block, result.stderr)


@pytest.mark.parametrize("readme", RUNBOOKS)
@pytest.mark.parametrize(
    "statuses, started",
    [([1, 75], False), ([1, 2], False), ([1, 1, 0], True), ([0], True)],
)
def test_timer_starts_only_after_a_complete_backfill(
    tmp_path, readme, statuses, started
):
    """Run steps 6 and 7 with a stand-in sync exiting with ``statuses`` in
    turn: the timer is started only if the last run exited 0, and otherwise
    a line says it is still stopped."""
    answers = tmp_path / "answers"
    answers.write_text("\n".join(map(str, statuses)) + "\n")
    calls = tmp_path / "calls"
    sync = tmp_path / "ibp-shipment-sync"
    sync.write_text(
        "#!/bin/bash\n"
        f'status=$(head -n1 "{answers}"); sed -i 1d "{answers}"; exit "$status"\n'
    )
    sync.chmod(0o755)
    script = (
        f'sudo() {{ echo "$*" >> "{calls}"; }}\nsleep() {{ :; }}\n'
        + backfill_steps(readme).replace("~/.local/bin/ibp-shipment-sync", str(sync))
    )
    result = subprocess.run(
        ["bash", "-c", script], text=True, capture_output=True, check=False
    )
    ran = calls.read_text() if calls.exists() else ""
    assert ("systemctl start ibp-shipment-sync.timer" in ran) is started
    assert ("TIMER STILL STOPPED" in result.stdout) is not started


@pytest.mark.parametrize("readme", RUNBOOKS)
def test_no_runbook_starts_the_timer_unconditionally(readme):
    """Every line starting or enabling the timer does so only on success."""
    for block in code_blocks(readme):
        for line in block.splitlines():
            if "ibp-shipment-sync.timer" in line and (
                "systemctl start" in line or "systemctl enable" in line
            ):
                assert line.lstrip().startswith("if "), line
