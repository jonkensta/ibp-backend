# Error digest

Weekly job on ibp-server that sums up the week's warnings and errors from
every IBP app in one short, redacted report, for an AI session to read
(the meta repo's `/ibp-errors` skill). The design, and the format the apps
write, is the meta repo's `docs/error-reporting.md`. It only reads logs: it
changes nothing but its own digest and state folders.

- `ibp-error-digest`: reads the last 7 days, groups the events, compares
  them with earlier weeks and writes `/srv/ibp-logs/digest/<YYYY>-W<ww>.md`
  and a `.json` copy. Python 3 stdlib only.
- `ibp-error-digest.service` / `.timer`: Thursdays 09:00 America/Chicago,
  `Persistent=true`, the day after the label refunds. Runs as **root**,
  because `jstarr` can't read the stations' folders, with
  `ProtectSystem=strict`: the only paths it can write are
  `/srv/ibp-logs/digest` and its state folder `/var/lib/ibp-error-digest`.

## What it reads

Each station's Samba share is its own folder, `/srv/ibp-logs/<station>/`.
A Windows laptop uploads into the folder itself (`IBP_LAPTOP/errors-shippy.jsonl`);
a kiosk into a folder named after its host (`uname -n`), with the print
server's logs and its journal export below it (kiosk `docs/apps.md`, "Log
upload"). Events from a laptop are shown as from the station folder, a
kiosk's as from its host.

| Source | Format |
|---|---|
| `<station>/errors-*.jsonl` (and rotated `.1`...), and a kiosk's `<station>/<host>/errors-*.jsonl` and `<station>/<host>/print-server/errors-*.jsonl` | the shared JSONL format (`ts`, `level`, `app`, `version`, `host`, `logger`, `msg`, `exc`, `data`) |
| `<station>/<host>/journal/journal-<day>.jsonl` | a kiosk's journal entries at warning or above, in journald's field names (`__REALTIME_TIMESTAMP`, `PRIORITY`, `MESSAGE`, `SYSLOG_IDENTIFIER`, `_SYSTEMD_UNIT`, `UNIT`, ...) |
| `<station>/<host>/journal/failed-units-<day>.jsonl` | the units failed at the kiosk's last upload that day (UTC), rewritten at every upload; empty or missing when none is. One journal-shaped line per unit: `SYSLOG_IDENTIFIER` `ibp-kiosk-log-upload`, `PRIORITY` `3`, `MESSAGE` `<unit> failed`, `UNIT` (system) or `USER_UNIT` (the kiosk user's), `__REALTIME_TIMESTAMP`. Each becomes one event a day, app `systemd`, `unit failed: <unit>` or `user unit failed: <unit>`, so a unit that stays failed counts every day the station runs |
| the same folders' `{printer,errors}-*.jsonl` | only the `<app> starting` lines (`shippy starting`, `label watcher starting`), and of those only `ts`, `app`, `host` and the version (`version`, or `data.version`), for each station's (a kiosk: each host's) app versions. The print server's starting line has no version |
| `/srv/ibp/logs/*.jsonl` | `backend.jsonl` (host `ibp-server`) and `client-errors.jsonl` (host: the client address, named by `/etc/ibp-error-digest/hosts`) |
| `journalctl -u 'ibp-*' -p warning` | the Pi's own jobs (backup, refunds, sync, this one), and `systemctl --failed` |

Events below WARNING are skipped. A line that isn't JSON, is over 256 KB,
is nested too deep to parse, isn't an event (no time stamp, or one outside
2000-2100), or has a field of a type that trips the reader, is counted as
unreadable, by file, in the report; no line stops a run. Only the first
4000 characters of a message are redacted (and at most 300 kept), and a
stack's lines are cut to 1000 characters. A folder that isn't there is noted, not
an error (`/srv/ibp/logs` exists from backend v1.0.22). Files are never
read through a symlink, and a file not modified since a day before the
week began is not read for events (its lines are older). `digest/` is not
a station.

## What it writes

1. **Groups** by fingerprint (12 hex digits): app, exception type and the
   innermost frame in IBP code (`file:function`, so a line number or a
   message changing doesn't make a new group; the frontend's bundle hash
   is dropped too), or, for an event with no exception, its redacted
   message with numbers and codes replaced.
2. **Status**, from `/var/lib/ibp-error-digest/seen.json` (fingerprint ->
   app, level, redacted message, first and last seen, weekly counts for 26
   weeks): **new** (no earlier week), **growing** (up by half or more on
   last week), **recurring**, or **gone** (last week, not this week).
   Running again in the same week replaces that week's counts.
3. **Muted** groups, `/etc/ibp-error-digest/muted` (`<fingerprint> <reason>`
   per line, `#` comments), are counted and listed with their reason but
   left out of the groups and statuses.
4. **Stations**: each folder's newest file time (the uploads keep the
   station's times, so this is when the station last wrote a log) and
   **no upload all week** when it is older than the week; each station app's
   version from its latest `starting` line, next to the `deploy` branch's
   (`shippy`, `printing`, `kiosk`, read from GitHub; `unknown` when it
   can't be) or the release's `IBP_TAG` (backend, frontend, extension).
5. **The report**'s first line says whether there are new or growing
   groups. Each group has its status, app, highest level, count, hosts,
   versions, first and last seen, last week's count, the redacted message
   and exception line, the logger, the stack as frames only (`file:line
   function`, the last 12, no paths above the package, no source lines or
   locals), and where one raw event is: `path:line` on the Pi, or a
   `journalctl` command. Digests older than 26 weeks are removed.

**Redaction.** No `data` field, request body or raw line is ever copied. In
each message and exception line: package and request codes keep only their
prefix (`TEX-########-#`), e-mail addresses become `<email>`, quoted
strings `<text>`, runs of four or more digits `<num>`, and everything after
a word containing `address`, `name` or `inmate` becomes `<redacted>`. Text
is one line, at most 300 characters. Of `.env` only the `IBP_TAG=` line is
read.

**Who can read it.** The digests are `root:adm`, mode `0640`, in a
`root:adm 0750` folder: `jstarr` and `emilliken` (both in `adm`) can copy
them, e.g. `scp jstarr@ibp-server:/srv/ibp-logs/digest/2026-W48.md .`,
which is what `/ibp-errors` does. The laptops' Samba accounts can't (their shares are
their own folders). `seen.json` is root-only. If the group is missing the
digests are root-only and the report says so.

Exit status: 0 the digest was written, 1 it could not be or a source could
not be read (the journal; the digest is still written), 2 bad arguments, 75
another run holds the lock (`/var/lib/ibp-error-digest/run.lock`; counted
as success by the service).

Monitoring: pings a Healthchecks check (start, then the exit status with a
short summary: the first line, counts, stale stations, problems; never a
message) if `/etc/ibp-error-digest/ping-url` (root 0600) exists. Without it
there is no ping.

## Install

Run on ibp-server from a checkout of the release, in this folder:

    sudo install -D -m755 ibp-error-digest /usr/local/bin/ibp-error-digest
    sudo install -d -m755 /etc/ibp-error-digest
    sudo install -d -o root -g adm -m750 /srv/ibp-logs/digest
    sudo cp ibp-error-digest.service ibp-error-digest.timer /etc/systemd/system/
    sudo systemctl daemon-reload

Optional, in `/etc/ibp-error-digest/` (root, 0644 unless said):

- `hosts`: `<address> <name>` per line, to name the browsers' addresses in
  `client-errors.jsonl` (the addresses are in the meta repo's `CLAUDE.md`
  hosts table, LAN and tailnet).
- `muted`: `<fingerprint> <reason>` per line.
- `ping-url` (0600): the Healthchecks check's ping URL,
  `(umask 077; echo https://... | sudo tee /etc/ibp-error-digest/ping-url >/dev/null)`.

Look at a dry run (prints the digest, writes and pings nothing), then
enable the timer and run it once:

    sudo /usr/local/bin/ibp-error-digest --dry-run | less
    sudo systemctl enable --now ibp-error-digest.timer
    sudo systemctl start ibp-error-digest.service && journalctl -u ibp-error-digest -n 20
    ls -l /srv/ibp-logs/digest/

To try it on another machine: `--logs-root`, `--server-logs`, `--state-dir`,
`--config-dir`, `--env-file`, `--group ''`, `--no-journal`, `--offline` and
`--now` point it elsewhere (`--help`). The tests are
`tests/test_error_digest.py`.
