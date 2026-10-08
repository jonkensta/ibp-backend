# data.db backups

Snapshot (Mon & Fri 13:00) of `~/ibp/data.db` on ibp-server, uploaded to Google Drive.

- `ibp-backup` — `sqlite3 .backup` → `integrity_check` → zstd → upload to `gdrive:backups/ibp/`.
  Keeps the newest 20 on Drive and the newest 4 locally in `~/backups/ibp/`.
- `ibp-backup.service` / `ibp-backup.timer` — systemd units.

Monitoring: the script pings the self-hosted Healthchecks check `ibp-backup`
(https://healthchecks.jstarr.me, on jstarr-beelink) on start and with its exit status plus the
log tail. It emails if a run fails or none arrives by Mon/Fri 15:00. The ping URL is read from
`~/.config/ibp-backup/ping-url` (mode 0600, kept out of the repo); without it the script just
skips the pings.

## Install

    sudo apt install rclone sqlite3 zstd
    install -m755 ibp-backup ~/.local/bin/
    sudo cp ibp-backup.service ibp-backup.timer /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now ibp-backup.timer

rclone remote `gdrive` (scope `drive.file`) lives in `~/.config/rclone/rclone.conf`. Create it on
a machine with a browser and copy the file over:

    rclone --config ibp-rclone.conf config create gdrive drive scope=drive.file >/dev/null
    scp ibp-rclone.conf ibp-server:.config/rclone/rclone.conf

## Restore

    rclone copy gdrive:backups/ibp/data-YYYY-MM-DD_HHMM.db.zst .
    zstd -d data-*.db.zst -o data.db
    sudo systemctl stop gunicorn && cp data.db ~/ibp/data.db && sudo systemctl start gunicorn
