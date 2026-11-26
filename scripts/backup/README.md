# data.db backups

Snapshot (Mon & Fri 13:00) of the IBP database on ibp-server, uploaded to Google Drive.

- `ibp-backup`: `sqlite3 .backup` → `integrity_check` → core-table check → zstd → upload to
  `gdrive:backups/ibp/`. Keeps the newest 20 on Drive and the newest 4 locally in `~/backups/ibp/`.
- `ibp-backup.service` / `ibp-backup.timer`: systemd units.

The script runs on the **host**, not in a container. It reads the database straight from the
Docker deployment's bind mount, `/srv/ibp/data/data.db` (see [`deploy/pi/`](../../deploy/pi/README.md)).
SQLite's locking works across the host and the container, so the online `.backup` is consistent
while the app runs. To back up a different file, add `Environment=IBP_DB=…` to the service.

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

Fetch and check the backup first; this touches nothing live:

    mkdir -p ~/restore && cd ~/restore
    rclone copy gdrive:backups/ibp/data-YYYY-MM-DD_HHMM.db.zst .   # or cp from ~/backups/ibp/
    zstd -d data-YYYY-MM-DD_HHMM.db.zst -o data.db
    sqlite3 data.db "PRAGMA integrity_check;"     # must print: ok

Then swap it in with the block below. Two timers start jobs that open the
database: the shipment sync (daily 07:30, which also migrates it on start)
and this backup (Mon & Fri 13:00). The block stops both timers, waits for a
run in progress, and holds the sync's run lock (see
[`scripts/shipment-sync/`](../shipment-sync/README.md)), so no job opens the
database while it is swapped; the label refund job never opens it. The live
database is kept, with its SQLite side files, in a new
`data/before-restore-…` directory; keep it until the restore is verified.

The block starts the timers that were running again at the end, once the
backend answers on the restored database. If it stops before the live
database was moved, it starts the backend and those timers again on the
live database. If it stops after, it leaves the timers stopped and says
where the live database is: fix the failed step and run the block again.

    (
      set -Eeuo pipefail
      trap 'echo "RESTORE STOPPED: the step above failed" >&2' ERR
      cd /srv/ibp
      # The timers running now, kept once: a re-run after a failure (which
      # left them stopped) still starts them at the end.
      was=.timers-before-restore
      if [ ! -e "$was" ]; then
        for timer in ibp-shipment-sync.timer ibp-backup.timer; do
          state=$(systemctl show -p ActiveState --value "$timer")
          if [ "$state" = active ]; then echo "$timer"; fi
        done > "$was.new"
        mv "$was.new" "$was"
      fi
      aside=""
      finish() {
        status=$?
        exec 9>&-    # release the sync's run lock, so a run the timer missed can start
        if [ -n "$aside" ] && [ "$status" -ne 0 ]; then
          echo "NOT RESTORED: the live database is in /srv/ibp/$aside (keep it: a re-run moves what is in data/ now to a new directory); the timers stay stopped; fix the failed step and run this block again" >&2
          return 0
        fi
        if [ -z "$aside" ]; then
          echo "NOT RESTORED: nothing was changed; starting the backend and the timers again" >&2
          docker compose start || true
        fi
        if [ -s "$was" ]; then
          xargs sudo systemctl start < "$was" || { echo "TIMERS NOT STARTED: run sudo systemctl start $(tr '\n' ' ' < "$was")" >&2; return 0; }
        fi
        rm -f "$was"
      }
      trap finish EXIT
      sudo systemctl stop ibp-shipment-sync.timer ibp-backup.timer
      # Wait for a run already in progress: the services (a running oneshot
      # is "activating") and any one-off backend container (a sync's). Each
      # query must succeed: a failed one stops the block instead of reading
      # as "nothing running".
      while :; do
        busy=no
        for service in ibp-shipment-sync.service ibp-backup.service; do
          state=$(systemctl show -p ActiveState --value "$service")
          case "$state" in active | activating | deactivating | reloading) busy=yes ;; esac
        done
        running=$(docker ps -q --filter label=com.docker.compose.oneoff=True --filter label=com.docker.compose.service=ibp-backend)
        if [ "$busy" = no ] && [ -z "$running" ]; then break; fi
        sleep 5
      done
      # Hold the sync's run lock (the wrapper's) for the rest of the block,
      # so no sync, not even one started by hand, can start until it is done.
      mkdir -p ~/.local/state/ibp-shipment-sync
      exec 9>>~/.local/state/ibp-shipment-sync/run.lock
      flock -n 9 || { echo "a shipment sync holds its run lock; wait for it to finish and run this block again" >&2; exit 1; }
      docker compose stop ibp-backend
      # A new directory for the live database and its side files, so a
      # re-run never overwrites one kept before.
      dir=data/before-restore-$EPOCHSECONDS-$$
      mkdir "$dir"
      aside=$dir
      for file in "" -journal -wal -shm; do
        if [ -e "data/data.db$file" ]; then mv "data/data.db$file" "$aside/data.db$file"; fi
      done
      install -m 0644 ~/restore/data.db data/data.db   # owned by you, the container's user
      docker compose start   # the backend, and the frontend if it stopped too
      # The backend migrates an older backup as it starts: no sync may start
      # before it answers.
      for _ in $(seq 60); do if curl -fs -o /dev/null localhost/api/health; then break; fi; sleep 2; done
      curl -fs localhost/api/health     # {"status":"ok","database":"connected"}
      echo
      echo "RESTORED; the database it replaced is in /srv/ibp/$aside"
    )

Anything written after the backup was taken is lost. This restores into the
running release; to go back to an older release whose schema is behind the
current database, use the [rollback steps](../../deploy/pi/README.md#rolling-back) in the
deploy README instead (the old release can't start on the newer schema).

A backup taken from the legacy app has the legacy schema; run it through `scripts/migrate.py`
first (see [Migrating from the legacy database](../../README.md#migrating-from-the-legacy-database)).
