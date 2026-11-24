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
         git clone https://github.com/jonkensta/ibp-backend.git ~/ibp-backend
         cp ~/ibp-backend/deploy/pi/{compose.yaml,nginx.conf} /srv/ibp/
         cp ~/ibp-backend/deploy/pi/.env.example /srv/ibp/.env
         "${EDITOR:-nano}" /srv/ibp/.env     # IBP_TAG, ISBN lookup keys
         chmod 600 /srv/ibp/.env
         cp /path/to/migrated.db /srv/ibp/data/data.db
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

    (
      set -Eeuo pipefail
      trap 'echo "UPGRADE STOPPED: fix the failed step and run this block again" >&2' ERR
      cd /srv/ibp
      cp -p .env .env.pre-<new tag>             # for a rollback
      "${EDITOR:-nano}" .env                     # new IBP_TAG
      sudo systemctl start ibp-backup.service   # a fresh backup first
      docker compose pull
      docker compose up -d
      docker image prune -f
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
      # 2. Wait for a run already in progress: a running oneshot service is
      #    "activating", which is-active alone doesn't count as running.
      while systemctl is-active --quiet ibp-shipment-sync.service || [ "$(systemctl show -p ActiveState --value ibp-shipment-sync.service)" = activating ]; do sleep 5; done
      # 3. ...and for any one-off sync container (an older wrapper's has no
      #    label of its own, so wait on every one-off backend container).
      while [ -n "$(docker ps -q --filter label=com.docker.compose.oneoff=True --filter label=com.docker.compose.service=ibp-backend)" ]; do sleep 5; done
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
upgrade first, with both containers stopped (writes since then are lost):

    (
      set -Eeuo pipefail
      trap 'echo "ROLLBACK STOPPED: the step above failed" >&2' ERR
      cd /srv/ibp
      # The containers must be stopped before anything else.
      docker compose down || { echo "NOT RESTORED: the containers did not stop; nothing was changed"; exit 1; }
      # A fresh staging file, so no older one can be taken for this backup.
      mkdir -p ~/restore
      staging=~/restore/data-$EPOCHSECONDS-$$.db
      rm -f "$staging"
      zstd -d ~/backups/ibp/data-<before upgrade>.db.zst -o "$staging" || { echo "NOT RESTORED: the backup did not decompress; nothing was changed"; exit 1; }
      check=$(sqlite3 "$staging" 'PRAGMA integrity_check;') || check="failed"
      [ "$check" = ok ] || { echo "NOT RESTORED: the backup failed its integrity check; nothing was changed; pick another backup"; exit 1; }
      # Only now is the live database replaced: the backup is copied in
      # beside it first, then the live one and its sidecar files are moved
      # aside (kept until verified) and the copy moved into place.
      install -m 0644 "$staging" data/data.db.restoring
      mv data/data.db data/data.db.before-rollback
      for sidecar in -journal -wal -shm; do
        if [ -e "data/data.db$sidecar" ]; then mv "data/data.db$sidecar" "data/data.db.before-rollback$sidecar"; fi
      done
      mv data/data.db.restoring data/data.db
      cp .env.pre-<new tag> .env                # the previous IBP_TAG
      docker compose up -d
      docker compose ps                         # both on the old tag, healthy
    )
