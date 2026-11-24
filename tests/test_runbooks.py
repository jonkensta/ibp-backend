"""The runbooks' shell blocks (deploy/pi/README.md, scripts/shipment-sync/
README.md), run with stand-in commands: each stops at its first failed
step with a non-zero status, and runs every step in order otherwise."""

# pylint: disable=redefined-outer-name

import re
import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

TOPLEVEL = Path(__file__).resolve().parent.parent
RUNBOOKS = ["deploy/pi/README.md", "scripts/shipment-sync/README.md"]

# Stand-ins for every command the blocks run. Each counted step logs its
# command line and fails if it is step $FAIL_AT. Queries a wait loop makes
# (systemctl is-active/show, docker ps) answer "nothing running" and are
# not steps; sudo runs its command; the shell an installer is piped into
# is not a step either (pipefail fails the pipeline on the download).
PRELUDE = r"""
step() {
  local n; read -r n < "$COUNT"; n=$((n + 1)); echo "$n" > "$COUNT"
  printf '%s\n' "$*" >> "$LOG"
  [ "$n" -ne "$FAIL_AT" ]
}
for command in apt curl git chmod install cp mv rm mkdir zstd journalctl cat cd \
    tailscale usermod nano; do
  eval "$command() { step $command \"\$@\"; }"
done
sh() { :; }
sleep() { :; }
sudo() { "$@"; }
docker() { if [ "$1" = ps ]; then return 0; fi; step docker "$@"; }
systemctl() {
  case "$1" in is-active) return 3 ;; show) echo inactive; return 0 ;; esac
  step systemctl "$@"
}
sqlite3() { step sqlite3 "$@" || return 1; printf '%s\n' "${SQLITE_OUT-ok}"; }
ibp_sync() {
  if [ -n "${ANSWERS:-}" ]; then
    local status; read -r status < "$ANSWERS"; command sed -i 1d "$ANSWERS"
    step ibp-shipment-sync "$@"; return "$status"
  fi
  step ibp-shipment-sync "$@" || return "${SYNC_FAIL:-2}"
}
"""


def code_blocks(readme: str) -> list[str]:
    """The indented code blocks of a Markdown file (4 spaces, or 7 in a
    numbered list), dedented by 4."""
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


def is_shell(block: str) -> bool:
    """Every block is shell but the illustrative compose.yaml snippet."""
    return "environment:" not in block


SHELL_BLOCKS = [
    pytest.param(readme, index, id=f"{readme}#{index}")
    for readme in RUNBOOKS
    for index, block in enumerate(code_blocks(readme))
    if is_shell(block)
]


class Result(NamedTuple):
    """How a block ran: its status, the steps it ran, its output."""

    status: int
    steps: list[str]
    output: str


def run_block(
    tmp_path: Path, block: str, fail_at: int = 0, env: Optional[dict] = None
) -> Result:
    """Run ``block`` with the stand-ins, failing step ``fail_at`` (0: none)."""
    home = tmp_path / "home"
    (home / ".config" / "ibp-shipment-sync").mkdir(parents=True, exist_ok=True)
    count, log = tmp_path / "count", tmp_path / "log"
    count.write_text("0\n")
    log.write_text("")
    # The placeholders are <...>; make them plain words, keep redirections.
    script = block.replace("~/.local/bin/ibp-shipment-sync --", "ibp_sync --")
    for placeholder in ("<new tag>", "<checkout of the new tag>", "<before upgrade>"):
        script = script.replace(placeholder, "X")
    result = subprocess.run(
        ["bash", "-c", PRELUDE + script],
        text=True,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "COUNT": str(count),
            "LOG": str(log),
            "FAIL_AT": str(fail_at),
            **(env or {}),
        },
    )
    # The rollback's staging file has a new name each run; compare runs
    # without it.
    steps = [
        re.sub(r"data-\d+-\d+\.db", "data-STAGING.db", step)
        for step in log.read_text().splitlines()
    ]
    return Result(result.returncode, steps, result.stdout + result.stderr)


def block_at(readme: str, index: int) -> str:
    """The ``index``-th code block of ``readme``."""
    return code_blocks(readme)[index]


@pytest.mark.parametrize("readme, index", SHELL_BLOCKS)
def test_block_is_valid_shell(readme, index):
    """Every shell block parses."""
    script = block_at(readme, index).replace("<", "_").replace(">", "_")
    result = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("readme, index", SHELL_BLOCKS)
def test_block_stops_at_its_first_failed_step(tmp_path, readme, index):
    """With every step succeeding the block exits 0 having run them all;
    failing any one step stops it there, with a non-zero status."""
    block = block_at(readme, index)
    success = run_block(tmp_path, block)
    assert success.status == 0, success.output
    assert success.steps
    for fail_at in range(1, len(success.steps) + 1):
        failed = run_block(tmp_path, block, fail_at)
        assert failed.status != 0, (fail_at, failed.output)
        assert failed.steps == success.steps[:fail_at], (fail_at, failed.output)


def rollback_block() -> str:
    """The rollback block of deploy/pi/README.md."""
    (block,) = [
        b for b in code_blocks("deploy/pi/README.md") if "docker compose down" in b
    ]
    return block


def test_rollback_runs_its_steps_in_order_on_one_fresh_file(tmp_path):
    """The containers stop first; the backup goes to a staging file removed
    just before, which the integrity check reads and which is copied in
    before the live database is moved aside."""
    steps = run_block(tmp_path, rollback_block()).steps
    names = [" ".join(step.split()[:3]) for step in steps]
    assert names[:2] == ["cd /srv/ibp", "docker compose down"]
    staging = steps[3].split()[-1]
    assert steps[3] == f"rm -f {staging}"
    assert steps[4].startswith("zstd -d ") and steps[4].endswith(f"-o {staging}")
    assert steps[5].startswith(f"sqlite3 {staging} ")
    assert steps[6] == f"install -m 0644 {staging} data/data.db.restoring"
    assert steps[7] == "mv data/data.db data/data.db.before-rollback"
    assert steps[8] == "mv data/data.db.restoring data/data.db"
    assert names[-3:] == [
        "cp .env.pre-X .env",
        "docker compose up",
        "docker compose ps",
    ]
    # A new name every run: no older file can be taken for this backup.
    assert "staging=~/restore/data-$EPOCHSECONDS-$$.db" in rollback_block()
    assert staging.endswith("/restore/data-STAGING.db")


@pytest.mark.parametrize("output", ["", "not ok", "ok\nok", "*** in database main ***"])
def test_rollback_needs_exactly_ok_from_the_integrity_check(tmp_path, output):
    """Anything but exactly "ok" stops the rollback before the live database
    is touched."""
    result = run_block(tmp_path, rollback_block(), env={"SQLITE_OUT": output})
    assert result.status != 0
    assert not [step for step in result.steps if step.split()[0] in {"mv", "install"}]
    assert "NOT RESTORED: the backup failed its integrity check" in result.output


@pytest.mark.parametrize(
    "failing, message",
    [
        ("docker compose down", "NOT RESTORED: the containers did not stop"),
        ("zstd", "NOT RESTORED: the backup did not decompress"),
    ],
)
def test_rollback_failures_leave_the_live_database_alone(tmp_path, failing, message):
    """A failed stop or decompression stops with its line, before any file
    of the live database is moved or removed."""
    success = run_block(tmp_path, rollback_block()).steps
    fail_at = next(n for n, step in enumerate(success, 1) if step.startswith(failing))
    result = run_block(tmp_path, rollback_block(), fail_at)
    assert result.status != 0 and message in result.output
    assert not [step for step in result.steps if "data/data.db" in step]


def backfill_block(readme: str) -> str:
    """The re-evaluation's backfill block (steps 6 and 7)."""
    (block,) = [b for b in code_blocks(readme) if "# 6. Re-evaluate" in b]
    return block


@pytest.mark.parametrize("readme", RUNBOOKS)
@pytest.mark.parametrize(
    "statuses, started",
    [([1, 75], False), ([1, 2], False), ([1, 1, 0], True), ([0], True)],
)
def test_timer_starts_only_after_a_complete_backfill(
    tmp_path, readme, statuses, started
):
    """With the sync exiting ``statuses`` in turn, the timer is started only
    if the last run exited 0; otherwise the block says the timer is still
    stopped and exits non-zero."""
    answers = tmp_path / "answers"
    answers.write_text("\n".join(map(str, statuses)) + "\n")
    result = run_block(tmp_path, backfill_block(readme), env={"ANSWERS": str(answers)})
    assert len([s for s in result.steps if s.startswith("ibp-shipment-sync")]) == len(
        statuses
    )
    assert ("systemctl start ibp-shipment-sync.timer" in result.steps) is started
    assert ("TIMER STILL STOPPED" in result.output) is not started
    assert (result.status == 0) is started


@pytest.mark.parametrize("readme", RUNBOOKS)
def test_no_runbook_starts_the_timer_unconditionally(readme):
    """Every line starting or enabling the timer is a branch of an ``if`` on
    the backfill's success."""
    for block in code_blocks(readme):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        for previous, line in zip(["", *lines], lines):
            if "ibp-shipment-sync.timer" in line and (
                "systemctl start" in line or "systemctl enable" in line
            ):
                assert line.startswith("if ") or previous.endswith("then"), line


@pytest.mark.parametrize("readme", RUNBOOKS)
def test_runbook_waits_for_activating_runs_and_installs_the_wrapper_first(readme):
    """The upgrade runbooks wait for a run that is still activating and for
    any one-off sync container, and install the new wrapper and service
    before the backfill, which comes before starting the timer."""
    text = (TOPLEVEL / readme).read_text()
    steps = [
        "sudo systemctl stop ibp-shipment-sync.timer",
        'ActiveState --value ibp-shipment-sync.service)" = activating',
        "--filter label=com.docker.compose.oneoff=True",
        "install -D -m755 ibp-shipment-sync ~/.local/bin/ibp-shipment-sync",
        "sudo systemctl daemon-reload",
        "--since 2025-11-01",
        "sudo systemctl start ibp-shipment-sync.timer",
    ]
    start = text.find("sudo systemctl stop ibp-shipment-sync.timer")
    positions = [text.find(step, start) for step in steps]
    assert -1 not in positions, dict(zip(steps, positions))
    assert positions == sorted(positions)
