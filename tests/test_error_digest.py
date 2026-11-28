"""Tests for scripts/error-digest/ibp-error-digest, on made-up log trees.

No test reaches the network or this machine's journal: the journal and
systemctl are a stand-in command, the deploy versions a stand-in fetch.
Every name, number and address below is invented.
"""

# pylint: disable=redefined-outer-name

import grp
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "error-digest"
    / "ibp-error-digest"
)
_loader = importlib.machinery.SourceFileLoader("ibp_error_digest", str(SCRIPT))
_spec = importlib.util.spec_from_loader("ibp_error_digest", _loader)
assert _spec is not None
digest = importlib.util.module_from_spec(_spec)
sys.modules["ibp_error_digest"] = digest  # dataclasses look the module up
_loader.exec_module(digest)

# Thursday 2026-11-26 09:00 in Austin: ISO week 48.
NOW = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
CT = timezone(timedelta(hours=-6))

# Invented inmate data that must never reach a digest.
SECRETS = [
    "Jane Q Example",
    "Example",
    "4521 Fake Street",
    "jane@example.org",
    "01234567",
    "TEX-01234567-2",
]

PY_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "C:\\Users\\insid\\AppData\\Local\\uv\\tools\\shippy\\Lib\\site-packages'
    '\\shippy\\gui\\workers\\shipment_worker.py", line {line}, in run\n'
    "    self.buy()\n"
    '  File "C:\\Users\\insid\\AppData\\Local\\uv\\tools\\shippy\\Lib\\site-packages'
    '\\shippy\\core\\easypost.py", line 88, in buy\n'
    "    return client.post(url)\n"
    '  File "C:\\Users\\insid\\AppData\\Local\\uv\\tools\\shippy\\Lib\\site-packages'
    '\\httpx\\_client.py", line 1100, in post\n'
    "    raise ConnectError(msg)\n"
    "httpx.ConnectError: {message}"
)


def event(ts, level="ERROR", msg="label purchase failed", **extra):
    return {
        "ts": ts.astimezone(CT).isoformat(timespec="milliseconds"),
        "level": level,
        "logger": "shippy.gui.workers.shipment_worker",
        "host": "IBP-PIPPIN",
        "pid": 4242,
        "thread": "MainThread",
        "attempt_id": "-",
        "msg": msg,
        **extra,
    }


def write_jsonl(path: Path, lines, mtime=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line if isinstance(line, str) else json.dumps(line))
            handle.write("\n")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


class FakeSystem:
    """journalctl and systemctl on a made-up Pi."""

    def __init__(self, entries=(), failed=(), journal_status=0):
        self.entries = list(entries)
        self.failed = list(failed)
        self.journal_status = journal_status
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "journalctl":
            out = "\n".join(json.dumps(e) for e in self.entries)
            return subprocess.CompletedProcess(argv, self.journal_status, out, "")
        out = "".join(f"{u} loaded failed failed Something\n" for u in self.failed)
        return subprocess.CompletedProcess(argv, 0, out, "")


def fake_fetch(url):
    versions = {"shippy": "0.2.0", "printing": "0.5.0", "kiosk": "0.1.0"}
    for repo, version in versions.items():
        if f"/{repo}/deploy/" in url:
            return f'[project]\nname = "x"\nversion = "{version}"\n'.encode()
    raise OSError("no such file")


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "srv-ibp-logs"
    server = tmp_path / "srv-ibp-logs-server"
    state = tmp_path / "state"
    config = tmp_path / "etc"
    for folder in (root, server, state, config):
        folder.mkdir()
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-not-real\nIBP_TAG=v1.0.22\n")
    return {
        "root": root,
        "server": server,
        "state": state,
        "config": config,
        "env": env,
        "digest": root / "digest",
    }


def options(tree, now=NOW, **overrides):
    return digest.Options(
        logs_root=tree["root"],
        server_logs=tree["server"],
        state_dir=tree["state"],
        config_dir=tree["config"],
        env_file=tree["env"],
        group=overrides.pop("group", ""),
        now=now,
        **overrides,
    )


def run(tree, now=NOW, system=None, fetch=fake_fetch, **overrides):
    return digest.digest(
        options(tree, now=now, **overrides), command=system or FakeSystem(), fetch=fetch
    )


def groups_by_app(report):
    return {g["app"]: g for g in report["groups"]}


# --------------------------------------------------------------------------
# Redaction


@pytest.mark.parametrize(
    "text, expected",
    [
        ("package TEX-01234567-2 failed", "package TEX-########-# failed"),
        ("fed FED-1234567-10 ok", "fed FED-#######-## ok"),
        ("mail jane@example.org bounced", "mail <email> bounced"),
        (
            "printer \"BOB\" offline and 'BETTY' too",
            "printer <text> offline and <text> too",
        ),
        ("can't open the printer", "can't open the printer"),
        (
            "lookup failed for inmate Jane Q Example",
            "lookup failed for inmate <redacted>",
        ),
        ("bad address: 4521 Fake Street", "bad address <redacted>"),
        ("unit_name=Hughes is odd", "unit_name <redacted>"),
        ("tracking 9400111202555842761234 stuck", "tracking <num> stuck"),
        ("retry 3 of 5 in 120 s", "retry 3 of 5 in 120 s"),
        ("unterminated 'Jane Example", "unterminated <text>"),
        ("two\nlines\there", "two lines here"),
    ],
)
def test_redact(text, expected):
    assert digest.redact(text) == expected


def test_redact_keeps_messages_short_and_ignores_non_text():
    assert len(digest.redact("x" * 1000)) == digest.MAX_TEXT
    assert digest.redact(None) == ""
    assert digest.redact({"name": "Jane"}) == ""


def test_keyword_tail_is_redacted_even_after_a_code():
    assert digest.redact("TEX-01234567-2 name Jane") == "TEX-########-# name <redacted>"


# --------------------------------------------------------------------------
# Stacks and fingerprints


def test_python_stack_frames_are_short_and_innermost_ibp_frame_found():
    stack = digest.parse_stack(PY_TRACE.format(line=41, message="host 'x' down"))
    assert [f.text() for f in stack.frames] == [
        "shippy/gui/workers/shipment_worker.py:41 run",
        "shippy/core/easypost.py:88 buy",
        "httpx/_client.py:1100 post",
    ]
    assert stack.type == "httpx.ConnectError"
    assert stack.message == "host <text> down"
    assert stack.innermost().text() == "shippy/core/easypost.py:88 buy"
    assert "insid" not in " ".join(f.text() for f in stack.frames)


def test_same_bug_has_one_fingerprint_whatever_the_message_and_line():
    one = digest.parse_stack(PY_TRACE.format(line=41, message="for TEX-01234567-2"))
    two = digest.parse_stack(PY_TRACE.format(line=43, message="timed out"))
    assert digest.fingerprint("shippy-gui", one, "a") == digest.fingerprint(
        "shippy-gui", two, "b"
    )
    assert digest.fingerprint("shippy-gui", one, "a") != digest.fingerprint(
        "shippy", one, "a"
    )


def test_chained_python_traceback_uses_the_last_exception():
    exc = (
        "Traceback (most recent call last):\n"
        '  File "/app/ibp/books.py", line 10, in lookup\n'
        "    x()\n"
        "KeyError: 'isbn'\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "/app/ibp/routes.py", line 20, in get_book\n'
        "    y()\n"
        "ValueError: bad value 'Jane'\n"
    )
    stack = digest.parse_stack(exc)
    assert stack.type == "ValueError"
    assert [f.text() for f in stack.frames] == ["ibp/routes.py:20 get_book"]
    assert stack.message == "bad value <text>"


def test_checkout_paths_with_ibp_in_them_are_not_mistaken_for_ibp_code():
    file, ibp = digest.python_file(
        "/home/x/Source/ibp/shippy/.venv/lib/python3.13/site-packages/httpx/_client.py"
    )
    assert (file, ibp) == ("httpx/_client.py", False)
    assert digest.python_file("/home/x/Source/ibp/backend/ibp/routes.py") == (
        "ibp/routes.py",
        True,
    )
    assert digest.python_file("/usr/local/bin/ibp-error-digest") == (
        "ibp-error-digest",
        True,
    )


def test_js_stacks_chrome_and_firefox_and_bundle_hashes():
    chrome = (
        "TypeError: Cannot read properties of undefined (reading 'name')\n"
        "    at renderRow (http://192.0.2.10/assets/index-BkX9a1_z.js:12:345)\n"
        "    at http://192.0.2.10/assets/vendor-Ab12Cd34.js:1:2\n"
    )
    stack = digest.parse_stack(chrome)
    assert stack.type == "TypeError"
    assert stack.frames[-1].text() == "assets/index.js:12 renderRow"
    assert stack.innermost().func == "renderRow"
    rebuilt = chrome.replace("BkX9a1_z", "Zz99yy00").replace(":12:345", ":14:1")
    assert digest.fingerprint("frontend", stack, "x") == digest.fingerprint(
        "frontend", digest.parse_stack(rebuilt), "y"
    )
    firefox = (
        "fillField@moz-extension://0000-1111/content.js:40:7\n"
        "onMessage@moz-extension://0000-1111/background.js:9:3\n"
    )
    stack = digest.parse_stack(firefox)
    assert stack.type == ""
    assert [f.text() for f in stack.frames] == [
        "background.js:9 onMessage",
        "content.js:40 fillField",
    ]
    assert stack.innermost().text() == "content.js:40 fillField"


def test_messages_without_exception_group_by_their_shape():
    one = digest.redact("printer BOB offline after 12 tries for TEX-01234567-2")
    two = digest.redact("printer BOB offline after 3 tries for TEX-7654321-0")
    other = digest.redact("printer BOB out of paper")
    assert digest.fingerprint("shippy", None, one) == digest.fingerprint(
        "shippy", None, two
    )
    assert digest.fingerprint("shippy", None, one) != digest.fingerprint(
        "shippy", None, other
    )
    assert digest.FINGERPRINT.fullmatch(digest.fingerprint("shippy", None, one))


# --------------------------------------------------------------------------
# A whole run


def station_logs(tree, folder="IBP_LAPTOP"):
    base = tree["root"] / folder
    write_jsonl(
        base / "errors-shippy-gui.jsonl",
        [
            event(
                NOW - timedelta(days=1),
                exc=PY_TRACE.format(line=41, message="TEX-01234567-2"),
                data={"name": "Jane Q Example", "address": "4521 Fake Street"},
                app="shippy-gui",
                version="0.1.0",
            ),
            event(
                NOW - timedelta(days=2),
                exc=PY_TRACE.format(line=43, message="timed out"),
                app="shippy-gui",
                version="0.1.0",
            ),
            event(NOW - timedelta(days=9), msg="too old"),  # before the window
            event(NOW - timedelta(hours=1), level="INFO", msg="not an error"),
            "{not json",
            "[1, 2, 3]",
            "",
            json.dumps({"level": "ERROR", "msg": "no time stamp"}),
        ],
    )
    write_jsonl(
        base / "errors-shippy-gui.jsonl.1",
        [
            event(
                NOW - timedelta(days=3),
                level="WARNING",
                msg="lookup failed for inmate Jane Q Example (01234567), mail jane@example.org",
                app="shippy-gui",
                version="0.1.0",
            )
        ],
    )
    write_jsonl(
        base / "printer-watcher.jsonl",
        [
            watcher_starting(NOW - timedelta(days=20), "0.4.0"),
            watcher_starting(NOW - timedelta(days=2), "0.5.0"),
            event(
                NOW - timedelta(days=1),
                level="INFO",
                msg="printing starting soon",
                app="watcher",
                data={"version": "9.9.9"},
            ),
        ],
    )
    write_jsonl(
        base / "printer-shippy.jsonl",
        [shippy_starting(NOW - timedelta(days=4), "shippy", "0.1.0")],
    )
    return base


def watcher_starting(ts, version):
    """ibp-printing's label watcher: app "watcher", the version in data."""
    return event(
        ts,
        level="INFO",
        msg="label watcher starting",
        app="watcher",
        logger="ibp_printing.apps.watcher",
        data={
            "version": version,
            "argv": ["ibp-label-watcher"],
            "config_file": "C:\\Users\\insid\\watcher.toml",
            "state_dir": "C:\\Users\\insid\\state",
        },
    )


def shippy_starting(ts, app, version):
    """shippy's start_logging: app and version at the top and in data."""
    return event(
        ts,
        level="INFO",
        msg=f"{app} starting",
        app=app,
        version=version,
        logger="shippy.startup",
        data={
            "app": app,
            "version": version,
            "argv": [app, "--request", "TEX-01234567-2"],
            "python": "3.12.7",
            "platform": "win32",
            "error_log": f"errors-{app}.jsonl",
        },
    )


def test_run_writes_a_redacted_digest(tree):
    station_logs(tree)
    status, report, markdown = run(tree)
    assert status == 0
    md = (tree["digest"] / "2026-W48.md").read_text()
    js = (tree["digest"] / "2026-W48.json").read_text()
    seen = (tree["state"] / "seen.json").read_text()
    assert md == markdown + "\n"
    assert json.loads(js) == report
    for text in (md, js, seen):
        for secret in SECRETS:
            assert secret not in text, secret
    assert md.splitlines()[0] == "# IBP error digest 2026-W48: 2 new - run /ibp-errors"


def test_run_groups_counts_and_describes_each_group(tree):
    station_logs(tree)
    _, report, _ = run(tree)
    assert report["summary"]["events"] == 3
    exc_group, msg_group = report["groups"]
    assert exc_group["count"] == 2 and exc_group["status"] == "new"
    assert exc_group["app"] == "shippy-gui"
    assert exc_group["hosts"] == {"IBP_LAPTOP": 2}
    assert exc_group["versions"] == ["0.1.0"]
    assert exc_group["exception"] == {
        "type": "httpx.ConnectError",
        "message": "TEX-########-#",
    }
    assert exc_group["frames"][-1] == "httpx/_client.py:1100 post"
    path, line = exc_group["raw_event"].rsplit(":", 1)
    assert path.endswith("IBP_LAPTOP/errors-shippy-gui.jsonl") and line == "1"
    assert msg_group["level"] == "WARNING"
    assert msg_group["message"] == "lookup failed for inmate <redacted>"
    assert msg_group["raw_event"].endswith("errors-shippy-gui.jsonl.1:1")
    # "{not json", "[1, 2, 3]" and the line with no time stamp.
    assert report["unreadable"] == {"IBP_LAPTOP/errors-shippy-gui.jsonl": 3}


def test_stations_and_versions(tree):
    station_logs(tree)
    old = station_logs(tree, "IBP_LAPTOP2")
    long_ago = NOW - timedelta(days=10)
    for path in old.iterdir():
        os.utime(path, (long_ago.timestamp(), long_ago.timestamp()))
    _, report, markdown = run(tree)
    stations = {s["folder"]: s for s in report["stations"]}
    assert stations["IBP_LAPTOP"]["stale"] is False
    assert stations["IBP_LAPTOP"]["hosts"] == ["IBP-PIPPIN"]
    assert stations["IBP_LAPTOP2"]["stale"] is True
    assert "**no upload all week**" in markdown
    versions = {(v["folder"], v["app"]): v for v in report["versions"]}
    assert versions[("IBP_LAPTOP", "watcher")]["version"] == "0.5.0"
    assert versions[("IBP_LAPTOP", "watcher")]["status"] == "current"
    assert versions[("IBP_LAPTOP", "shippy")]["status"] == "old"
    assert versions[("IBP_LAPTOP", "shippy")]["expected"] == "0.2.0"
    # The stale station's events are older than the window's margin: unread.
    assert report["summary"]["events"] == 3


def test_versions_unknown_offline(tree):
    station_logs(tree)
    _, report, _ = run(tree, fetch=None)
    assert {v["status"] for v in report["versions"]} == {"unknown"}


def test_digest_folder_and_symlinks_are_not_read(tree, tmp_path):
    outside = write_jsonl(
        tmp_path / "elsewhere.jsonl", [event(NOW - timedelta(hours=2), msg="planted")]
    )
    base = tree["root"] / "IBP_LAPTOP"
    base.mkdir()
    (base / "errors-shippy.jsonl").symlink_to(outside)
    (tree["root"] / "linked").symlink_to(base)
    write_jsonl(tree["digest"] / "errors-x.jsonl", [event(NOW, msg="in digest")])
    _, report, _ = run(tree)
    assert report["groups"] == []
    assert [s["folder"] for s in report["stations"]] == ["IBP_LAPTOP"]


def test_missing_folders_still_give_a_digest(tree):
    tree["root"].rmdir()
    tree["server"].rmdir()
    status, report, _ = run(tree)
    assert status == 0
    assert any("not there" in p for p in report["problems"])
    assert (tree["digest"] / "2026-W48.md").exists()


def usec(moment):
    return str(int(moment.timestamp() * 1_000_000))


def failed_unit_line(moment, unit, kind="UNIT"):
    """A failed-units-<day>.jsonl line as ibp-kiosk-log-upload writes it."""
    return {
        "__REALTIME_TIMESTAMP": usec(moment),
        "_HOSTNAME": "ibp-bilbo",
        "SYSLOG_IDENTIFIER": "ibp-kiosk-log-upload",
        "PRIORITY": "3",
        "MESSAGE": f"{unit} failed",
        kind: unit,
    }


def kiosk_host(tree, station="IBP_KIOSK1", host="ibp-bilbo"):
    """A kiosk's upload: <share>/<host>/ inside the station's folder."""
    return tree["root"] / station / host


def test_kiosk_station_layout(tree):
    host = kiosk_host(tree)
    write_jsonl(
        host / "errors-shippy.jsonl",
        [
            event(
                NOW - timedelta(days=1),
                msg="printer BOB offline",
                app="shippy",
                host="ibp-bilbo",
            )
        ],
    )
    write_jsonl(
        host / "printer-watcher.jsonl",
        [{**watcher_starting(NOW - timedelta(days=3), "0.4.0"), "host": "ibp-bilbo"}],
    )
    write_jsonl(
        host / "print-server" / "errors-print-server.jsonl",
        [
            event(
                NOW - timedelta(hours=6),
                msg="DYMO not found",
                app="print-server",
                host="ibp-bilbo",
            )
        ],
    )
    write_jsonl(
        host / "print-server" / "printer-print-server.jsonl",
        [
            # The print server's starting line has no version: no row.
            event(
                NOW - timedelta(days=2),
                level="INFO",
                msg="print server starting",
                host="ibp-bilbo",
            )
        ],
    )
    write_jsonl(
        host / "journal" / "journal-2026-11-25.jsonl",
        [
            {
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(days=1)),
                "PRIORITY": "3",
                "SYSLOG_IDENTIFIER": "ibp-kiosk",
                "_HOSTNAME": "ibp-bilbo",
                "MESSAGE": list(b"tile portal failed for inmate Jane"),
            },
            {
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(days=1)),
                "PRIORITY": "6",
                "SYSLOG_IDENTIFIER": "ibp-kiosk",
                "MESSAGE": "just info",
            },
            {
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(days=2)),
                "PRIORITY": "4",
                "_SYSTEMD_UNIT": "ibp-kiosk-update.service",
                "MESSAGE": "update check slow",
            },
            {
                # The uploader's own warning: not a failed unit.
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(days=2)),
                "PRIORITY": "4",
                "SYSLOG_IDENTIFIER": "ibp-kiosk-log-upload",
                "MESSAGE": "journal export failed; going on with the upload",
            },
            "garbage",
        ],
    )
    _, report, markdown = run(tree)
    apps = groups_by_app(report)
    assert apps["shippy"]["hosts"] == {"ibp-bilbo": 1}
    assert apps["shippy"]["raw_event"].endswith(
        "IBP_KIOSK1/ibp-bilbo/errors-shippy.jsonl:1"
    )
    assert apps["print-server"]["hosts"] == {"ibp-bilbo": 1}
    assert apps["ibp-kiosk"]["level"] == "ERROR"
    assert apps["ibp-kiosk"]["message"] == "tile portal failed for inmate <redacted>"
    assert apps["ibp-kiosk-update"]["level"] == "WARNING"
    assert apps["ibp-kiosk-log-upload"]["level"] == "WARNING"
    assert "systemd" not in apps
    assert report["unreadable"] == {
        "IBP_KIOSK1/ibp-bilbo/journal/journal-2026-11-25.jsonl": 1
    }
    assert report["stations"] == [
        {
            "folder": "IBP_KIOSK1",
            "hosts": ["ibp-bilbo"],
            "last_upload": digest.iso(NOW),
            "stale": False,
        }
    ]
    assert [
        (v["folder"], v["app"], v["version"], v["status"]) for v in report["versions"]
    ] == [("IBP_KIOSK1/ibp-bilbo", "watcher", "0.4.0", "old")]
    assert "IBP_KIOSK1 | ibp-bilbo" in markdown


def test_kiosk_failed_units_count_once_a_day_while_failed(tree):
    journal = kiosk_host(tree) / "journal"
    days = {day: NOW - timedelta(days=day) for day in range(9)}

    def failed_file(day, lines):
        moment = days[day]
        name = f"failed-units-{moment.astimezone(timezone.utc).date()}.jsonl"
        return write_jsonl(journal / name, lines, mtime=moment)

    # The update has been failed for three days; the label watcher (a user
    # unit) only today, listed twice; yesterday nothing had failed (an empty
    # file); the day before, the older format's "no failed units" line.
    for day in (0, 1, 3, 4):
        lines = [failed_unit_line(days[day], "ibp-kiosk-update.service")]
        if day == 0:
            lines += [
                failed_unit_line(days[0], "ibp-label-watcher.service", "USER_UNIT")
            ] * 2
        if day == 1:
            lines = []
        failed_file(day, lines)
    failed_file(
        2,
        [
            {
                "__REALTIME_TIMESTAMP": usec(days[2]),
                "_HOSTNAME": "ibp-bilbo",
                "SYSLOG_IDENTIFIER": "ibp-kiosk-log-upload",
                "PRIORITY": "6",
                "MESSAGE": "no failed units",
            }
        ],
    )
    # Before the week: not counted, and the file isn't read.
    failed_file(8, [failed_unit_line(days[8], "ibp-kiosk-update.service")])
    _, report, _ = run(tree)
    failed = {g["message"]: g for g in report["groups"] if g["app"] == "systemd"}
    assert set(failed) == {
        "unit failed: ibp-kiosk-update.service",
        "user unit failed: ibp-label-watcher.service",
    }
    assert failed["unit failed: ibp-kiosk-update.service"]["count"] == 3
    assert failed["unit failed: ibp-kiosk-update.service"]["hosts"] == {"ibp-bilbo": 3}
    assert failed["user unit failed: ibp-label-watcher.service"]["count"] == 1
    assert report["summary"]["events"] == 4
    assert report["unreadable"] == {}


def test_station_folder_layouts_side_by_side(tree):
    station_logs(tree)  # a laptop: files in the folder itself
    write_jsonl(
        kiosk_host(tree) / "journal" / f"failed-units-{NOW.date()}.jsonl",
        [failed_unit_line(NOW - timedelta(hours=1), "ibp-kiosk-update.service")],
    )
    (tree["root"] / "IBP_KIOSK1" / "linked").symlink_to(tree["root"] / "IBP_LAPTOP")
    _, report, _ = run(tree)
    assert [s["folder"] for s in report["stations"]] == ["IBP_KIOSK1", "IBP_LAPTOP"]
    assert groups_by_app(report)["systemd"]["hosts"] == {"ibp-bilbo": 1}
    # The laptop's events are counted once: the symlink isn't followed.
    assert report["summary"]["events"] == 4


# --------------------------------------------------------------------------
# Malformed input never stops a run


def test_bad_lines_and_field_types_are_unreadable_not_fatal(tree):
    deep = "[" * 100_000 + "]" * 100_000
    base = tree["root"] / "IBP_LAPTOP"
    write_jsonl(
        base / "errors-shippy.jsonl",
        [
            deep,
            "x" * (digest.MAX_LINE + 10),  # too long: skipped, not held
            event(NOW - timedelta(hours=1), msg={"not": "text"}, exc=5, level=3),
            event(NOW - timedelta(hours=1), msg=["a"], exc={"b": 1}, logger=[1]),
            json.dumps({"ts": "9999-12-31T23:59:59-12:00", "level": "ERROR"}),
            json.dumps({"ts": ["2026-11-25"], "level": "ERROR"}),
            event(NOW - timedelta(hours=2), msg="still read"),
        ],
    )
    write_jsonl(
        base / "printer-shippy.jsonl",
        [
            deep.replace("[", '[ "starting", ', 1),
            event(NOW, level="INFO", msg="shippy starting", data=["x"], version=7),
            json.dumps(
                {
                    "ts": "0001-01-01T00:00:00+05:00",
                    "msg": "shippy starting",
                    "version": "0.1.0",
                }
            ),
            shippy_starting(NOW - timedelta(days=1), "shippy", "0.2.0"),
        ],
    )
    journal = kiosk_host(tree) / "journal"
    write_jsonl(
        journal / "journal-2026-11-25.jsonl",
        [
            deep,
            '{"MESSAGE": "x", "PRIORITY": 1e400, "__REALTIME_TIMESTAMP": "'
            + usec(NOW)
            + '"}',
            {"MESSAGE": "x", "PRIORITY": "3", "__REALTIME_TIMESTAMP": "9" * 30},
            {
                "MESSAGE": "x",
                "PRIORITY": "3",
                "__REALTIME_TIMESTAMP": usec(NOW),
                "SYSLOG_IDENTIFIER": ["ibp-kiosk"],
                "_HOSTNAME": {"a": 1},
            },
        ],
    )
    write_jsonl(
        journal / f"failed-units-{NOW.date()}.jsonl",
        [
            {
                "SYSLOG_IDENTIFIER": "ibp-kiosk-log-upload",
                "PRIORITY": "3",
                "MESSAGE": ["?"],
                "UNIT": {"a": 1},
                "__REALTIME_TIMESTAMP": usec(NOW),
            },
            [1, 2],
        ],
    )
    write_jsonl(
        tree["server"] / "client-errors.jsonl",
        [
            event(NOW - timedelta(hours=1), app="frontend", host=["192.0.2.1"]),
            event(NOW - timedelta(hours=1), app="frontend", host={"a": 1}),
            deep,
        ],
    )
    system = FakeSystem(
        entries=[{"MESSAGE": "x", "PRIORITY": 1e300, "__REALTIME_TIMESTAMP": usec(NOW)}]
    )
    system.entries.append(["not", "an", "entry"])
    status, report, _ = run(tree, system=system)
    assert status == 0
    assert report["unreadable"] == {
        "IBP_KIOSK1/ibp-bilbo/journal/failed-units-2026-11-26.jsonl": 1,
        "IBP_KIOSK1/ibp-bilbo/journal/journal-2026-11-25.jsonl": 2,
        "IBP_LAPTOP/errors-shippy.jsonl": 4,
        "client-errors.jsonl": 1,
    }
    apps = groups_by_app(report)
    assert apps["frontend"]["hosts"] == {"?": 2}
    assert apps["frontend"]["count"] == 2
    shippy = [g for g in report["groups"] if g["app"] == "shippy"]
    # Level 3 isn't a level: read, but not an event.
    assert sum(g["count"] for g in shippy) == 2
    assert {g["message"] for g in shippy} >= {"still read", ""}
    # The kiosk's lines with odd field types still count, under safe names.
    assert apps["journal"]["count"] == 1
    assert apps["ibp-kiosk-log-upload"]["count"] == 1
    versions = {(v["folder"], v["app"]): v["version"] for v in report["versions"]}
    assert versions == {("IBP_LAPTOP", "shippy"): "0.2.0"}


def test_long_messages_and_stacks_are_cut_before_redaction(tree):
    message = "a" * 50_000 + " TEX-01234567-2 " + "Jane " * 1000
    assert digest.redact(message).startswith("aaa")
    assert len(digest.redact(message)) <= digest.MAX_TEXT
    cut = digest.redact("word " * 799 + "TEX-01234567-2", limit=10_000)
    assert "0123" not in cut
    stack = PY_TRACE.format(line=1, message="x") + "\n" + " " * 20_000 + "y"
    assert digest.parse_stack(stack) is not None


def test_deploy_versions_survive_any_fetch_error():
    import http.client  # pylint: disable=import-outside-toplevel

    def broken(url):
        if "/shippy/" in url:
            raise http.client.IncompleteRead(b"[proj")
        if "/printing/" in url:
            return b"\xff\xfe not toml"
        return b"project = 5\n"

    assert not digest.deploy_versions(broken)


def test_ping_survives_any_error(monkeypatch, capsys):
    import http.client  # pylint: disable=import-outside-toplevel

    def broken(*_, **__):
        raise http.client.BadStatusLine("")

    monkeypatch.setattr(digest.urllib.request, "urlopen", broken)
    digest.ping("https://hc.example/ping/abc", "/0", "body")
    assert "healthchecks ping failed: BadStatusLine" in capsys.readouterr().err


def test_server_logs_and_client_addresses(tree):
    (tree["config"] / "hosts").write_text("# address name\n192.0.2.26 ibp-pippin\n")
    write_jsonl(
        tree["server"] / "client-errors.jsonl",
        [
            event(
                NOW - timedelta(hours=5),
                msg="render failed",
                app="frontend",
                host="192.0.2.26",
                version="1.0.22",
                exc="TypeError: x is undefined\n    at f (http://192.0.2.1/assets/index-AAAAAAAA.js:1:2)",
            ),
            event(
                NOW - timedelta(hours=4),
                msg="render failed",
                app="frontend",
                host="192.0.2.99",
                version="1.0.22",
                exc="TypeError: y is undefined\n    at f (http://192.0.2.1/assets/index-AAAAAAAA.js:1:9)",
            ),
        ],
    )
    write_jsonl(
        tree["server"] / "backend.jsonl",
        [
            event(
                NOW - timedelta(hours=1),
                msg="ISBNdb timed out",
                app="backend",
                host="3f2a1b0c9d8e",
            )
        ],
    )
    write_jsonl(tree["server"] / "notes.txt", ["not a log"])
    _, report, _ = run(tree)
    apps = groups_by_app(report)
    assert apps["frontend"]["hosts"] == {"192.0.2.99": 1, "ibp-pippin": 1}
    assert apps["frontend"]["count"] == 2
    assert apps["backend"]["hosts"] == {"ibp-server": 1}


def test_pi_journal_and_failed_units(tree):
    usec = str(int((NOW - timedelta(days=1)).timestamp() * 1_000_000))
    system = FakeSystem(
        entries=[
            {
                "__REALTIME_TIMESTAMP": usec,
                "PRIORITY": "4",
                "SYSLOG_IDENTIFIER": "systemd",
                "_SYSTEMD_UNIT": "init.scope",
                "UNIT": "ibp-backup.service",
                "MESSAGE": "ibp-backup.service: Failed with result 'exit-code'.",
            }
        ],
        failed=["ibp-label-refund.service"],
    )
    status, report, _ = run(tree, system=system)
    assert status == 0
    apps = groups_by_app(report)
    assert (
        apps["ibp-backup"]["message"]
        == "ibp-backup.service: Failed with result <text>."
    )
    assert apps["ibp-backup"]["raw_event"].startswith(
        "journalctl -u ibp-backup.service "
    )
    assert apps["systemd"]["message"] == "unit failed: ibp-label-refund.service"
    assert apps["systemd"]["raw_event"] == "systemctl status ibp-label-refund.service"
    journalctl = system.calls[0]
    assert journalctl[journalctl.index("-u") + 1] == "ibp-*"
    assert journalctl[journalctl.index("-p") + 1] == "warning"


def test_unreadable_journal_fails_the_run_but_writes_the_digest(tree):
    status, report, _ = run(tree, system=FakeSystem(journal_status=1))
    assert status == 1
    assert "Pi journal: journalctl exited 1" in report["problems"]
    assert (tree["digest"] / "2026-W48.md").exists()


# --------------------------------------------------------------------------
# Week to week


def week_of_errors(tree, now, count, msg="printer BOB offline"):
    write_jsonl(
        tree["root"] / "IBP_LAPTOP" / "errors-shippy.jsonl",
        [
            event(now - timedelta(hours=i + 1), msg=msg, app="shippy")
            for i in range(count)
        ],
        mtime=now,
    )


def statuses(report):
    return [(g["status"], g["count"], g["last_week"]) for g in report["groups"]]


def test_new_recurring_growing_gone(tree):
    week = timedelta(days=7)
    week_of_errors(tree, NOW, 2)
    assert statuses(run(tree)[1]) == [("new", 2, 0)]
    week_of_errors(tree, NOW + week, 2)
    assert statuses(run(tree, now=NOW + week)[1]) == [("recurring", 2, 2)]
    week_of_errors(tree, NOW + 2 * week, 3)
    _, report, markdown = run(tree, now=NOW + 2 * week)
    assert statuses(report) == [("growing", 3, 2)]
    assert markdown.splitlines()[0].endswith(": 1 growing - run /ibp-errors")
    (tree["root"] / "IBP_LAPTOP" / "errors-shippy.jsonl").unlink()
    _, report, markdown = run(tree, now=NOW + 3 * week)
    assert report["groups"] == []
    assert [(g["app"], g["last_week"]) for g in report["gone"]] == [("shippy", 3)]
    assert markdown.splitlines()[0].endswith("nothing new or growing")
    assert "## Gone" in markdown


def test_rerun_in_the_same_week_does_not_double_count(tree):
    week_of_errors(tree, NOW, 2)
    run(tree)
    _, report, _ = run(tree, now=NOW + timedelta(hours=2))
    assert statuses(report) == [("new", 2, 0)]
    seen = json.loads((tree["state"] / "seen.json").read_text())
    (entry,) = seen["fingerprints"].values()
    assert entry["weeks"] == {"2026-W48": 2}


def test_unreadable_seen_file_counts_everything_as_new(tree):
    (tree["state"] / "seen.json").write_text("{broken")
    week_of_errors(tree, NOW, 1)
    _, report, _ = run(tree)
    assert statuses(report) == [("new", 1, 0)]
    assert any("seen.json" in p for p in report["problems"])


def test_muted_groups_are_counted_not_shown(tree):
    week_of_errors(tree, NOW, 4)
    fp = run(tree, dry_run=True)[1]["groups"][0]["fingerprint"]
    (tree["config"] / "muted").write_text(
        f"# fingerprint reason\n{fp} BOB unplugged on purpose\nnot-a-fingerprint x\n"
    )
    _, report, markdown = run(tree)
    assert report["groups"] == []
    assert report["muted"] == [
        {
            "fingerprint": fp,
            "app": "shippy",
            "count": 4,
            "reason": "BOB unplugged on purpose",
        }
    ]
    assert report["summary"]["muted_events"] == 4
    assert any("not a fingerprint" in p for p in report["problems"])
    assert "## Muted" in markdown
    seen = json.loads((tree["state"] / "seen.json").read_text())
    assert seen["fingerprints"][fp]["weeks"] == {"2026-W48": 4}


def test_dry_run_writes_nothing(tree):
    week_of_errors(tree, NOW, 1)
    run(tree, dry_run=True)
    assert not tree["digest"].exists()
    assert not (tree["state"] / "seen.json").exists()


def test_old_digests_are_pruned_after_26_weeks(tree):
    tree["digest"].mkdir()
    for name in ("2026-W22.md", "2026-W22.json", "2026-W23.md", "notes.txt"):
        (tree["digest"] / name).write_text("x")
    run(tree)
    names = sorted(p.name for p in tree["digest"].iterdir())
    assert names == ["2026-W23.md", "2026-W48.json", "2026-W48.md", "notes.txt"]


def test_old_weeks_leave_seen_json(tree):
    old = {
        "app": "shippy",
        "level": "ERROR",
        "message": "m",
        "first_seen": "",
        "last_seen": "",
    }
    state = {
        "version": 1,
        "fingerprints": {
            "aaaaaaaaaaaa": {**old, "weeks": {"2026-W10": 3}},
            "bbbbbbbbbbbb": {**old, "weeks": {"2026-W10": 3, "2026-W40": 1}},
        },
    }
    (tree["state"] / "seen.json").write_text(json.dumps(state))
    run(tree)
    seen = json.loads((tree["state"] / "seen.json").read_text())["fingerprints"]
    assert list(seen) == ["bbbbbbbbbbbb"]
    assert seen["bbbbbbbbbbbb"]["weeks"] == {"2026-W40": 1}


def test_files_are_readable_by_the_reader_group_only(tree):
    group = grp.getgrgid(os.getgid()).gr_name
    run(tree, group=group)
    for name in ("2026-W48.md", "2026-W48.json"):
        info = (tree["digest"] / name).stat()
        assert info.st_mode & 0o777 == 0o640
        assert info.st_gid == os.getgid()
    assert (tree["state"] / "seen.json").stat().st_mode & 0o777 == 0o600


def test_missing_reader_group_leaves_files_root_only(tree):
    _, report, _ = run(tree, group="no-such-group-ibp")
    assert (tree["digest"] / "2026-W48.md").stat().st_mode & 0o777 == 0o600
    assert any("no-such-group-ibp" in p for p in report["problems"])


def test_release_is_read_from_ibp_tag_only(tree):
    assert digest.read_release(tree["env"]) == "v1.0.22"
    tree["env"].write_text("IBP_TAG=$(rm -rf /)\n")
    assert digest.read_release(tree["env"]) is None
    assert digest.read_release(tree["env"].parent / "missing") is None


# --------------------------------------------------------------------------
# main(): lock and Healthchecks


def main_args(tree, *extra):
    return [
        "--logs-root", str(tree["root"]),
        "--server-logs", str(tree["server"]),
        "--state-dir", str(tree["state"]),
        "--config-dir", str(tree["config"]),
        "--env-file", str(tree["env"]),
        "--group", "",
        "--no-journal",
        "--offline",
        "--now", NOW.isoformat(),
        *extra,
    ]  # fmt: skip


def test_main_without_ping_url_pings_nothing(tree, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        digest.urllib.request, "urlopen", lambda *a, **k: calls.append(a)
    )
    assert digest.main(main_args(tree)) == 0
    assert calls == []
    assert "nothing new or growing" in capsys.readouterr().out


def test_main_pings_start_and_status(tree, monkeypatch):
    (tree["config"] / "ping-url").write_text("https://hc.example/ping/abc\n")
    week_of_errors(tree, NOW, 1)
    urls = []
    monkeypatch.setattr(
        digest.urllib.request, "urlopen", lambda req, **k: urls.append(req.full_url)
    )
    assert digest.main(main_args(tree)) == 0
    assert urls == [
        "https://hc.example/ping/abc/start",
        "https://hc.example/ping/abc/0",
    ]


def test_main_refuses_a_second_run(tree):
    lock = digest.take_lock(tree["state"] / digest.LOCK_NAME)
    try:
        assert digest.main(main_args(tree)) == digest.BUSY
    finally:
        lock.close()


def test_main_dry_run_prints_the_digest(tree, capsys):
    week_of_errors(tree, NOW, 1)
    assert digest.main(main_args(tree, "--dry-run")) == 0
    assert capsys.readouterr().out.startswith("# IBP error digest 2026-W48: 1 new")
    assert not tree["digest"].exists()
