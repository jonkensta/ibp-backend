# Native deployment on ibp-server

Production runs without Docker on `ibp-server`: a Raspberry Pi 3 with arm64
Debian 12, Python 3.11 and 906 MB of RAM. nginx on port 80 serves the
prebuilt frontend and proxies `/api/` to a single uvicorn worker on
`127.0.0.1:8000`. Access is limited to the LAN and the tailnet. There is no
authentication.

| File | Installed as |
|---|---|
| `server.conf.example` | `/home/jstarr/ibp-new/server.conf` (mode 0600) |
| `ibp-backend.service` | `/etc/systemd/system/ibp-backend.service` |
| `nginx/ibp.conf` | `/etc/nginx/sites-available/ibp`, symlinked from `sites-enabled` |

Layout on the Pi:

| Path | What |
|---|---|
| `/home/jstarr/ibp-new` | Backend git checkout, `.venv`, `server.conf` and `data.db` |
| `/srv/ibp/www` | Frontend build (`dist/`). It lives outside `/home/jstarr` because the home directory is mode 0700, so nginx (`www-data`) cannot read it |
| `/var/log/nginx/ibp.*.log` | nginx logs, rotated by Debian's logrotate config |
| journal | Backend logs: `journalctl -u ibp-backend` |

## How `/api` fits together

The app is created with `root_path="/api"` (`ibp/base.py`). The nginx
`location /api/` uses `proxy_pass http://127.0.0.1:8000;` with no URI part,
so nginx forwards the path unchanged: `/api/health` reaches uvicorn as
`/api/health`. The app strips the `/api` prefix before routing, and the
interactive docs at `/api/docs` load `/api/openapi.json`. Don't add a
trailing slash or path to `proxy_pass`, and don't pass `--root-path` to
uvicorn. Either would strip or add the prefix a second time.

The health check is therefore the same on the Pi and through nginx:

    curl -s 127.0.0.1:8000/api/health    # on the Pi, straight to uvicorn
    curl -s http://ibp-server/api/health # through nginx
    # {"status":"ok","database":"connected"}

The frontend's API client defaults to the same-origin `/api`. Build it
without `VITE_API_BASE_URL` set.

## Server prep (no downtime)

These steps match DEPLOYMENT-PLAN F1–F6. None of them affect the running
legacy app.

### F1. Packages and uv

    sudo apt install curl fonts-dejavu-core sqlite3
    curl -LsSf https://astral.sh/uv/install.sh | sh   # installs ~/.local/bin/uv

The backend needs these at runtime:

- `curl`: the TDCJ and FBOP providers (`ibp/providers/misc.py`) query the
  inmate sites by running `curl` as a subprocess.
- `fonts-dejavu-core` (or the `fonts-dejavu` metapackage): label rendering
  (`ibp/labels.py`) loads `DejaVuSansMono.ttf` by name with Pillow, which
  finds it under `/usr/share/fonts`.
- `sqlite3`: not used by the app, but needed for snapshots, integrity checks
  and the backup job.

nginx is already installed on the legacy box.

### F2. Backend checkout and venv

    git clone https://github.com/jonkensta/ibp-backend.git ~/ibp-new
    cd ~/ibp-new
    git checkout <release-tag>
    UV_PYTHON_DOWNLOADS=never uv sync --locked --no-dev --compile-bytecode \
        --python /usr/bin/python3.11

This builds `.venv` on the system Python 3.11. `uv run` is not used in
production. The unit calls `.venv/bin/uvicorn` directly.

Write the config:

    install -m 0600 deploy/server.conf.example server.conf
    $EDITOR server.conf   # check the database path; no secrets are needed

The template leaves `[logging] logfile` empty so that logs go to the journal.
That needs the logging change from to-do B1. On a commit without it, set
`logfile` to an absolute path under `/home/jstarr/ibp-new`.

Smoke-test on port 8001 against a copy of the migrated database, while
legacy keeps running on port 80:

    cp /path/to/migrated.db ~/ibp-new/smoke.db
    sed 's#/ibp-new/data.db#/ibp-new/smoke.db#' server.conf > smoke.conf
    CONF=$PWD/smoke.conf .venv/bin/python -m ibp.db_bootstrap
    CONF=$PWD/smoke.conf .venv/bin/uvicorn ibp.base:app --host 127.0.0.1 --port 8001

In a second shell:

    curl -s 127.0.0.1:8001/api/health
    curl -s '127.0.0.1:8001/api/inmates?query=<a TDCJ ID>'   # exercises curl
    curl -s -o /tmp/label.png -w '%{http_code}\n' \
        127.0.0.1:8001/api/inmates/<jur>/<id>/requests/<index>/label  # exercises fonts
    free -h

Stop uvicorn with Ctrl-C, then remove the copies:
`rm -f smoke.conf smoke.db smoke.db-*`.

### F3. Frontend

Build on a desktop, not the Pi, with Node 20.19 or later:

    cd ibp-frontend
    git checkout <release-tag>
    # VITE_API_BASE_URL must be unset (check .env*), so the app calls /api
    npm ci && npm run build

Create the web root on the Pi once:

    sudo install -d -o jstarr -g jstarr -m 0755 /srv/ibp /srv/ibp/www

Then copy the build over from the desktop. Run this again after each
frontend release:

    rsync -rlt --delete --chmod=D755,F644 dist/ ibp-server:/srv/ibp/www/

The files are owned by `jstarr` and world-readable, which is all nginx needs.

### F4. nginx (prepare only)

The legacy box uses one monolithic `/etc/nginx/nginx.conf`. The new setup
uses Debian's stock `nginx.conf`, which includes `sites-enabled/*`, plus the
`ibp` site. Prepare everything without switching:

    sudo cp -a /etc/nginx/nginx.conf /etc/nginx/nginx.conf.legacy
    ls -l /etc/nginx/sites-enabled/ | tee ~/sites-enabled.legacy.txt

    # Debian's stock nginx.conf, from the nginx-common package
    cd /tmp && apt-get download nginx-common
    dpkg-deb --fsys-tarfile nginx-common_*.deb | tar -xO ./etc/nginx/nginx.conf \
        > ~/nginx.conf.debian

    sudo install -m 0644 ~/ibp-new/deploy/nginx/ibp.conf /etc/nginx/sites-available/ibp

    # Validate the new combination without touching the live config
    sed 's#/etc/nginx/sites-enabled/\*#/etc/nginx/sites-available/ibp#' \
        ~/nginx.conf.debian > /tmp/nginx-ibp-test.conf
    sudo nginx -t -c /tmp/nginx-ibp-test.conf

`nginx -t` succeeds while legacy nginx holds port 80. If it fails on
`listen [::]:80` because IPv6 is disabled on the Pi, delete that line from
the site file.

### F5. Disk

    df -h /

The venv, database copies and the archive take about 0.5 GB of the 1.5 GB
free. Move the `~/ibp/data.db.pre-*` snapshots off the box first.

### F6. Install the unit, disabled

    sudo install -m 0644 ~/ibp-new/deploy/ibp-backend.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemd-analyze verify /etc/systemd/system/ibp-backend.service
    ss -ltn | grep ':8000 ' || echo "port 8000 free"

Do not enable it yet.

The unit runs `python -m ibp.db_bootstrap` before uvicorn starts, so every
start or restart applies pending alembic migrations, as the Docker entrypoint
did. Its hardening makes the filesystem read-only except
`/home/jstarr/ibp-new`, where SQLite creates `data.db` and its `-journal`/
`-wal` files. If the database moves, update `ReadWritePaths` in the unit as
well as `server.conf`. `MemoryMax=300M` caps a runaway process. The app
peaks at about 70 MB.

## Cutover steps that use these files

Numbers follow the cutover checklist in DEPLOYMENT-PLAN.md.

4. Put the migrated database in place:

       install -m 0640 data.db /home/jstarr/ibp-new/data.db
       rm -f /home/jstarr/ibp-new/data.db-journal /home/jstarr/ibp-new/data.db-wal

   `server.conf` already points at this path.

5. Start the backend and check it:

       sudo systemctl enable --now ibp-backend
       curl -s 127.0.0.1:8000/api/health   # {"status":"ok","database":"connected"}
       journalctl -u ibp-backend -n 50

6. Switch nginx:

       sudo rm -f /etc/nginx/sites-enabled/default   # also a default_server
       sudo ln -s /etc/nginx/sites-available/ibp /etc/nginx/sites-enabled/ibp
       sudo install -m 0644 ~/nginx.conf.debian /etc/nginx/nginx.conf
       sudo nginx -t && sudo systemctl reload nginx
       curl -s http://ibp-server/api/health

   Check `http://ibp-server/`, `http://ibp-server.local/` and
   `http://192.168.1.156/` from a client.

10. Point the backup job at `/home/jstarr/ibp-new/data.db`. See
    `scripts/backup/`.

## Rollback

1. Stop the new backend:

       sudo systemctl disable --now ibp-backend

2. Restore the legacy nginx config:

       sudo cp /etc/nginx/nginx.conf.legacy /etc/nginx/nginx.conf
       sudo rm -f /etc/nginx/sites-enabled/ibp
       # re-create anything listed in ~/sites-enabled.legacy.txt, e.g. default:
       # sudo ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
       sudo nginx -t && sudo systemctl reload nginx

3. Start legacy again: `sudo systemctl enable --now gunicorn.socket`. The
   legacy `~/ibp/data.db` was only read during cutover.

The new install in `~/ibp-new` and `/srv/ibp/www` can stay in place for the
next attempt.

## Upgrading later

    sudo systemctl start ibp-backup.service     # fresh snapshot first
    cd ~/ibp-new && git fetch && git checkout <new-tag>
    UV_PYTHON_DOWNLOADS=never uv sync --locked --no-dev --compile-bytecode \
        --python /usr/bin/python3.11
    sudo systemctl restart ibp-backend          # runs migrations, then starts
    curl -s 127.0.0.1:8000/api/health

If the unit or site file changed, reinstall it as in F4/F6. Run
`sudo systemctl daemon-reload` after a unit change and
`sudo nginx -t && sudo systemctl reload nginx` after a site change. For a
frontend release, rerun the `rsync` from F3. No restart is needed.
