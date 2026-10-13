# data.db backups

Snapshot (Mon & Fri 13:00) of the IBP database on ibp-server, uploaded to Google Drive.

- `ibp-backup` — `sqlite3 .backup` → `integrity_check` → core-table check → zstd → upload to
  `gdrive:backups/ibp/`. Keeps the newest 20 on Drive and the newest 4 locally in `~/backups/ibp/`.
- `ibp-backup.service` / `ibp-backup.timer` — systemd units.

The script runs on the **host**, not in a container. It reads the database straight from the
Docker deployment's bind mount, `/srv/ibp/data/data.db` (see `deploy/pi/`). SQLite's locking
works across the host and the container, so the online `.backup` is consistent while the app
runs. Set `IBP_DB` to back up a different file; on the legacy Pi 3B that is
`IBP_DB=/home/jstarr/ibp/data.db` (add `Environment=IBP_DB=…` to the service).

Monitoring: the script pings the self-hosted Healthchecks check `ibp-backup`
(https://healthchecks.jstarr.me, on jstarr-beelink) on start and with its exit status plus the
log tail. It emails if a run fails or none arrives by Mon/Fri 15:00. The ping URL is read from
`~/.config/ibp-backup/ping-url` (mode 0600, kept out of the repo); without it the script just
skips the pings.

## Install

    sudo apt install rclone sqlite3 zstd curl
    install -D -m755 ibp-backup ~/.local/bin/ibp-backup
    sudo cp ibp-backup.service ibp-backup.timer /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now ibp-backup.timer

When moving to a new server, copy `~/.config/rclone/rclone.conf` and
`~/.config/ibp-backup/ping-url` from the old one, then run the job once by hand and check that
Healthchecks goes green:

    sudo systemctl start ibp-backup.service && journalctl -u ibp-backup -n 20

Only one server may run the timer at a time; disable it on the old one
(`sudo systemctl disable --now ibp-backup.timer`).

rclone remote `gdrive` (scope `drive.file`) lives in `~/.config/rclone/rclone.conf`. To create
it from scratch, do so on a machine with a browser and copy the file over:

    rclone --config ibp-rclone.conf config create gdrive drive scope=drive.file >/dev/null
    scp ibp-rclone.conf ibp-server:.config/rclone/rclone.conf

## Restore

    rclone copy gdrive:backups/ibp/data-YYYY-MM-DD_HHMM.db.zst .
    zstd -d data-YYYY-MM-DD_HHMM.db.zst -o data.db
    sqlite3 data.db "PRAGMA integrity_check;"     # must print: ok

    cd /srv/ibp
    docker compose stop ibp-backend
    mv data/data.db data/data.db.before-restore   # keep until the restore is verified
    rm -f data/data.db-journal data/data.db-wal data/data.db-shm
    install -m 0644 ~/data.db data/data.db        # owned by you, the container's user
    docker compose start ibp-backend
    curl -s localhost/api/health                   # {"status":"ok","database":"connected"}

A backup taken from the legacy app has the legacy schema; run it through
`scripts/migrate.py` first (see DEPLOYMENT-PLAN).
