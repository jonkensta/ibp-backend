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
| `/srv/ibp/.env` | `IBP_TAG=v…` (from `.env.example`) |
| `/srv/ibp/data/data.db` | the database, owned by `jstarr` (uid 1000) |
| `~/.local/bin/ibp-backup` + timer | backups, see `scripts/backup/README.md` |

Logs: `docker compose logs -f` (rotated by Docker: 3 × 10 MB per container).

## Setup

1. Raspberry Pi OS Lite 64-bit on the SSD (Raspberry Pi Imager: hostname,
   user `jstarr`, SSH key). Update the bootloader for USB boot first if the
   Pi doesn't boot from it. Use a blue USB 3 port; `lsusb` should show the
   ASMedia adapter and `dmesg | grep -i uas` should show no errors.

2. Packages, Docker and Tailscale:

       sudo apt update && sudo apt full-upgrade -y
       sudo apt install -y sqlite3 zstd rclone curl git
       curl -fsSL https://get.docker.com | sudo sh
       sudo usermod -aG docker jstarr        # log out and back in
       curl -fsSL https://tailscale.com/install.sh | sh
       sudo tailscale up

3. The stack:

       sudo install -d -o jstarr -g jstarr /srv/ibp /srv/ibp/data
       git clone https://github.com/jonkensta/ibp-backend.git ~/ibp-backend
       cp ~/ibp-backend/deploy/pi/{compose.yaml,nginx.conf} /srv/ibp/
       cp ~/ibp-backend/deploy/pi/.env.example /srv/ibp/.env
       $EDITOR /srv/ibp/.env                  # IBP_TAG
       cp /path/to/migrated.db /srv/ibp/data/data.db
       cd /srv/ibp && docker compose pull && docker compose up -d
       docker compose ps                      # both "healthy"
       curl -s localhost/api/health           # {"status":"ok","database":"connected"}

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
database and are edited on the app's Settings page. To override any
`sample.conf` value, mount a `server.conf` and point `CONF` at it:

    # compose.yaml, ibp-backend:
    environment:
      CONF: /config/server.conf
    volumes:
      - ./server.conf:/config/server.conf:ro

## Upgrading

    cd /srv/ibp
    $EDITOR .env                              # new IBP_TAG
    sudo systemctl start ibp-backup.service  # fresh backup first
    docker compose pull && docker compose up -d
    docker image prune -f

Roll back by setting the previous tag and running `docker compose up -d`
again. If the new release ran a database migration, also restore the backup
taken just before (`scripts/backup/README.md`).
