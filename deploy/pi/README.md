# Docker deployment on ibp-server (Raspberry Pi 4B)

Production runs as two containers on a Raspberry Pi 4B (arm64, Raspberry Pi
OS Lite 64-bit, booting from a USB SSD):

| Service | Image | What it does |
|---|---|---|
| `ibp-frontend` | `ghcr.io/jonkensta/ibp-frontend` | nginx on port 80: serves the built React app and proxies `/api/` to the backend |
| `ibp-backend` | `ghcr.io/jonkensta/ibp-backend` | FastAPI (uvicorn) on port 8000, only reachable from the frontend container |

GitHub Actions builds both images for amd64 and arm64 on every push to
`main` and on `v*` tags (`.github/workflows/docker.yml` in each repo), so
nothing is built on the Pi. Production pins a release tag in `.env`.

Access is the LAN and the tailnet only. There is no authentication and no
HTTPS.

## Layout on the Pi

| Path | What |
|---|---|
| `/srv/ibp/compose.yaml`, `nginx.conf` | copied from this directory |
| `/srv/ibp/.env` | `IBP_TAG=v…` and the ISBN lookup keys (from `.env.example`; mode 0600) |
| `/srv/ibp/data/data.db` | the database, owned by `jstarr` (uid 1000) |
| `~/.local/bin/ibp-backup` + timer | backups, see `scripts/backup/README.md` |

Logs: `docker compose logs -f` (rotated by Docker: 3 × 10 MB per container).

## Setup

1. Raspberry Pi OS Lite 64-bit on the SSD (Raspberry Pi Imager: hostname,
   user `jstarr`, SSH key). Update the bootloader for USB boot first if the
   Pi doesn't boot from it. Use a blue USB 3 port; `lsusb` should show the
   ASMedia adapter and `dmesg | grep -i uas` should show no errors.

2. Packages, Docker and Tailscale:

       (
         set -Eeuo pipefail
         trap 'echo "SETUP STOPPED: fix the failed step and run this block again" >&2' ERR
         sudo apt update
         sudo apt full-upgrade -y
         sudo apt install -y sqlite3 zstd rclone curl git
         curl -fsSL https://get.docker.com | sudo sh
         sudo usermod -aG docker jstarr        # log out and back in after
         curl -fsSL https://tailscale.com/install.sh | sh
         sudo tailscale up
       )

3. The stack:

       (
         set -Eeuo pipefail
         trap 'echo "SETUP STOPPED: fix the failed step and run this block again" >&2' ERR
         sudo install -d -o jstarr -g jstarr /srv/ibp /srv/ibp/data
         # A re-run keeps what an earlier attempt made: the checkout, the
         # edited .env and the database (which the backend may have written).
         [ -d ~/ibp-backend ] || git clone https://github.com/jonkensta/ibp-backend.git ~/ibp-backend
         cp ~/ibp-backend/deploy/pi/{compose.yaml,nginx.conf} /srv/ibp/
         [ -e /srv/ibp/.env ] || cp ~/ibp-backend/deploy/pi/.env.example /srv/ibp/.env
         "${EDITOR:-nano}" /srv/ibp/.env     # IBP_TAG, ISBN lookup keys
         chmod 600 /srv/ibp/.env
         [ -e /srv/ibp/data/data.db ] || cp /path/to/migrated.db /srv/ibp/data/data.db
         cd /srv/ibp
         docker compose pull
         docker compose up -d
         docker compose ps                      # both "healthy"
         curl -fs localhost/api/health          # {"status":"ok","database":"connected"}
       )

   The backend runs `ibp.db_bootstrap` on every start: on an empty `data/`
   it creates a fresh schema; on a migrated database it only applies pending
   alembic migrations.

   GHCR packages are private when first published. Either make both
   packages public (GitHub → Packages → package settings → visibility), or
   `docker login ghcr.io` on the Pi with a token that has `read:packages`.

4. Backups: see `scripts/backup/README.md`.

## Configuration

The backend needs no config file: the image's `sample.conf` defaults are the
production values (INFO logging to stderr, 5 s provider timeout, 60/90 day
warnings, 24 h cache), and the database path comes from `DATABASE_URL` in
`compose.yaml`. The return address and the unit mailroom line live in the
database and are edited on the app's Settings page. To change any
`sample.conf` value, mount a `server.conf` holding just the changed options
(it is read over `sample.conf`, so anything it leaves out keeps the default)
and point `CONF` at it; the backend refuses to start if `CONF` names a
missing file:

    # compose.yaml, ibp-backend:
    environment:
      CONF: /config/server.conf
    volumes:
      - ./server.conf:/config/server.conf:ro

## Upgrading

The block stops the shipment timer and holds the sync's run lock while it
runs, so no sync can start on the new image and migrate the database before
the backup; it starts the timer again at the end if it was running before.
It keeps the first saved copy of the previous `.env` if it is run again.

    (
      set -Eeuo pipefail
      trap 'echo "UPGRADE STOPPED: fix the failed step and run this block again (the shipment timer stays stopped until it completes)" >&2' ERR
      cd /srv/ibp
      # Save the previous .env once: a re-run after .env was edited must
      # not replace the saved copy with the new tag.
      saved=.env.pre-<new tag>
      if [ ! -e "$saved" ]; then cp -p .env "$saved"; fi
      saved_tag=$(sed -n 's/^IBP_TAG=//p' "$saved")
      if [ -z "$saved_tag" ] || [ "$saved_tag" = "<new tag>" ]; then
        echo "NOT UPGRADED: $saved names ${saved_tag:-no IBP_TAG}, not the version to roll back to; fix it by hand first"
        exit 1
      fi
      # No sync may start until the new version is up: one on the new image
      # would migrate the database before the backup. The timer's state is
      # kept, once, to restore it at the end.
      timer_was=.shipment-timer-before-<new tag>
      if [ ! -e "$timer_was" ]; then
        timer_state=$(systemctl show -p ActiveState --value ibp-shipment-sync.timer)
        printf '%s\n' "$timer_state" > "$timer_was"
      fi
      sudo systemctl stop ibp-shipment-sync.timer
      # Wait for a sync already running: the service (a running oneshot is
      # "activating") and any one-off backend container (an older wrapper's has
      # no label of its own). Each query must succeed: a failed one stops the
      # block instead of reading as "nothing running".
      while :; do
        state=$(systemctl show -p ActiveState --value ibp-shipment-sync.service)
        case "$state" in active | activating | deactivating | reloading) sleep 5; continue ;; esac
        running=$(docker ps -q --filter label=com.docker.compose.oneoff=True --filter label=com.docker.compose.service=ibp-backend)
        if [ -z "$running" ]; then break; fi
        sleep 5
      done
      # Hold the sync's run lock (the wrapper's) for the rest of the block, so
      # no sync can start until it is done.
      mkdir -p ~/.local/state/ibp-shipment-sync
      exec 9>>~/.local/state/ibp-shipment-sync/run.lock
      flock -n 9 || { echo "NOT UPGRADED: a shipment sync holds its run lock; wait for it to finish and run this block again"; exit 1; }
      "${EDITOR:-nano}" .env                     # new IBP_TAG
      sudo systemctl start ibp-backup.service    # a fresh backup first
      docker compose pull
      docker compose up -d
      docker image prune -f
      read -r timer_state < "$timer_was"
      if [ "$timer_state" = active ]; then sudo systemctl start ibp-shipment-sync.timer; fi
      rm -f "$timer_was"
    )

The links between requests and labels (`package_shipments`) are stored, and
the delivery chart reads them as stored. After an upgrade to a release that
changes only the link rules (`ibp/shipment_links.py`), rebuild them before
trusting the chart, rather than waiting for the next nightly sync:

    ~/.local/bin/ibp-shipment-sync --rebuild-links || { echo "LINKS NOT REBUILT: the chart still shows the old links; run it again"; false; }

A release that changes unit matching (`ibp/unit_places.py`,
`ibp/unit_matching.py`, `ibp/label_reading.py`) needs every stored label
matched again instead, which also rebuilds the links. Do it in this order
around the plain upgrade above (it is step 5): stopping the
timer doesn't stop a run in progress, and the old wrapper takes no lock, so
it is replaced before the backfill. A run that fails part-way says the
re-evaluation is incomplete and leaves the links as they were
(`scripts/shipment-sync/README.md`).

    (
      set -Eeuo pipefail
      trap 'echo "STOPPED before the upgrade: fix the failed step and run this block again; the timer stays stopped" >&2' ERR
      # 1. Stop the timer, so no new daily run starts.
      sudo systemctl stop ibp-shipment-sync.timer
      # 2.-3. Then:
      # Wait for a sync already running: the service (a running oneshot is
      # "activating") and any one-off backend container (an older wrapper's has
      # no label of its own). Each query must succeed: a failed one stops the
      # block instead of reading as "nothing running".
      while :; do
        state=$(systemctl show -p ActiveState --value ibp-shipment-sync.service)
        case "$state" in active | activating | deactivating | reloading) sleep 5; continue ;; esac
        running=$(docker ps -q --filter label=com.docker.compose.oneoff=True --filter label=com.docker.compose.service=ibp-backend)
        if [ -z "$running" ]; then break; fi
        sleep 5
      done
      # 4. Install the new release's wrapper and units (with its run lock)
      #    before anything else runs the sync; from a checkout of the new tag:
      cd <checkout of the new tag>/scripts/shipment-sync
      install -D -m755 ibp-shipment-sync ~/.local/bin/ibp-shipment-sync
      sudo cp ibp-shipment-sync.service ibp-shipment-sync.timer /etc/systemd/system/
      sudo systemctl daemon-reload
    )

Then upgrade the backend (`deploy/pi/README.md`, Upgrading: backup, new
`IBP_TAG`, `docker compose pull`, `docker compose up -d`), and only then:

    (
      set -Eeuo pipefail
      trap 'echo "STOPPED: the step above failed; the timer stays stopped" >&2' ERR
      # 6. Re-evaluate every label from the first label's date until it exits
      #    0; it is re-run only after an incomplete run (1); any other status
      #    (2 no key, 75 another run) stops the loop to be looked at.
      while true; do status=0; ~/.local/bin/ibp-shipment-sync --since 2025-11-01 || status=$?; [ "$status" -eq 1 ] || break; echo "not complete; again in 60 s"; sleep 60; done
      # 7. Start the timer again, only if the backfill completed (exit 0).
      if [ "$status" = 0 ]; then
        sudo systemctl start ibp-shipment-sync.timer
      else
        echo "TIMER STILL STOPPED: the backfill exited $status; run this block again until it exits 0; do not start the timer before"
        exit 1
      fi
    )

The new wrapper's runs never overlap: each holds a lock
(`~/.local/state/ibp-shipment-sync/run.lock`), and one that finds it held
exits 75 at once ("another ibp-shipment-sync run is in progress") without
running or pinging Healthchecks. Its container is labelled
`ibp.job=shipment-sync` (`docker ps --filter label=ibp.job=shipment-sync`).

Roll back by setting the previous tag and running `docker compose up -d`
again. If the new release ran a database migration, the old backend can't
start on the migrated database, so restore the backup taken just before the
upgrade with the block below (writes since then are lost, but the database
it replaces is kept, with its SQLite side files, in a new
`data/before-rollback-…` directory). It stops
the shipment timer and holds the sync's run lock throughout, so no sync can
migrate the restored database; it checks everything that can fail before
moving any file, and it can be run again after a failure. It leaves the
timer stopped:

    (
      set -Eeuo pipefail
      trap 'echo "ROLLBACK STOPPED: the step above failed; the shipment timer stays stopped" >&2' ERR
      cd /srv/ibp
      # Every check that can fail comes before anything is moved.
      previous=.env.pre-<new tag>
      [ -s "$previous" ] || { echo "NOT ROLLED BACK: $previous is missing or empty; nothing was changed"; exit 1; }
      previous_tag=$(sed -n 's/^IBP_TAG=//p' "$previous")
      [ -n "$previous_tag" ] || { echo "NOT ROLLED BACK: $previous names no IBP_TAG; nothing was changed"; exit 1; }
      # No sync may start on the new image and migrate the restored database.
      sudo systemctl stop ibp-shipment-sync.timer
      # Wait for a sync already running: the service (a running oneshot is
      # "activating") and any one-off backend container (an older wrapper's has
      # no label of its own). Each query must succeed: a failed one stops the
      # block instead of reading as "nothing running".
      while :; do
        state=$(systemctl show -p ActiveState --value ibp-shipment-sync.service)
        case "$state" in active | activating | deactivating | reloading) sleep 5; continue ;; esac
        running=$(docker ps -q --filter label=com.docker.compose.oneoff=True --filter label=com.docker.compose.service=ibp-backend)
        if [ -z "$running" ]; then break; fi
        sleep 5
      done
      # Hold the sync's run lock (the wrapper's) for the rest of the block, so
      # no sync can start until it is done.
      mkdir -p ~/.local/state/ibp-shipment-sync
      exec 9>>~/.local/state/ibp-shipment-sync/run.lock
      flock -n 9 || { echo "NOT ROLLED BACK: a shipment sync holds its run lock; wait for it to finish and run this block again"; exit 1; }
      # The containers must be stopped.
      docker compose down || { echo "NOT RESTORED: the containers did not stop; nothing was changed"; exit 1; }
      # A fresh staging file, so no older one can be taken for this backup.
      mkdir -p ~/restore
      staging=~/restore/data-$EPOCHSECONDS-$$.db
      rm -f "$staging"
      zstd -d ~/backups/ibp/data-<before upgrade>.db.zst -o "$staging" || { echo "NOT RESTORED: the backup did not decompress; nothing was changed"; exit 1; }
      check=$(sqlite3 "$staging" 'PRAGMA integrity_check;') || check="failed"
      [ "$check" = ok ] || { echo "NOT RESTORED: the backup failed its integrity check; nothing was changed; pick another backup"; exit 1; }
      # Stage the new files beside the live ones, and a new directory for
      # the live database (never one that holds a database a re-run could
      # overwrite). An attempt stopped part-way through the swap, after it
      # moved data.db aside, left its directory's name in the marker: its
      # remaining SQLite files (-wal, -shm) go to that same directory, so
      # the kept database stays whole.
      install -m 0644 "$staging" data/data.db.restoring
      cp "$previous" .env.restoring
      marker=data/.rollback-in-progress
      aside=""
      if [ -s "$marker" ] && [ ! -e data/data.db ]; then
        read -r aside < "$marker"
        if [ ! -e "$aside/data.db" ]; then aside=""; fi
      fi
      if [ -z "$aside" ]; then
        aside=data/before-rollback-$EPOCHSECONDS-$$
        mkdir "$aside"
        cp -p .env "$aside/.env"
        printf '%s\n' "$aside" > "$marker"
      fi
      # The swap: renames only, never onto a file already kept.
      for file in "" -journal -wal -shm; do
        if [ -e "data/data.db$file" ]; then
          [ ! -e "$aside/data.db$file" ] || { echo "NOT ROLLED BACK: $aside/data.db$file already exists; nothing more was moved"; exit 1; }
          mv "data/data.db$file" "$aside/data.db$file"
        fi
      done
      mv data/data.db.restoring data/data.db
      mv .env.restoring .env
      rm -f "$marker"
      docker compose up -d
      docker compose ps                         # both on the old tag, healthy
      echo "ROLLED BACK to $previous_tag; the database it replaced is in /srv/ibp/$aside. The shipment timer is still stopped: start it (sudo systemctl start ibp-shipment-sync.timer) only once the old version is confirmed healthy, and first reinstall the old wrapper and units from a checkout of $previous_tag if the rollback crosses the release that changed them."
    )
