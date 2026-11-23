# Shipment sync

Daily job on ibp-server that copies every EasyPost postage label and its
USPS tracking events into the IBP database, then rebuilds the links between
requests and labels (`shipments`, `tracking_events`, `package_shipments`;
see "Shipment tracking" in the top-level README). GET requests only: it
never buys, refunds or changes anything at EasyPost.

- `ibp-shipment-sync`: runs `python -m ibp.shipment_sync` in a one-off
  backend container (`docker compose run --rm` in `/srv/ibp`), with the key
  file mounted read-only into that container only. Arguments are passed
  through. Python 3 stdlib only.
- `ibp-shipment-sync.service` / `.timer`: daily 07:30, `Persistent=true`.
  That keeps clear of the label refunds (Wed 10:00) and backups (Mon, Fri
  13:00). A missed or failed run is caught up by the next one: each run
  lists the last 35 days.

Each run logs one line of counts, e.g.
`shipment sync: 412 labels seen, 11 new, 37 updated, 160 events added, 388 trackers polled (0 failed), 1420 links built`.
No names or addresses are logged or stored.

Exit status: 0 ok, 1 an EasyPost or database error (or a tracker it could
not fetch), 2 no usable key. Monitoring: pings a Healthchecks check (start,
then the exit status with the output) if `~/.config/ibp-shipment-sync/ping-url`
exists.

## Install

Needs a backend release with `ibp.shipment_sync` deployed, and `jstarr` in
the `docker` group (`id -nG`).

    install -D -m755 ibp-shipment-sync ~/.local/bin/ibp-shipment-sync
    install -d -m700 ~/.config/ibp-shipment-sync
    # production key, from Bitwarden app.easypost.com field api-key-production
    (umask 077; cat > ~/.config/ibp-shipment-sync/easypost-api-key)
    # optional: (umask 077; echo https://... > ~/.config/ibp-shipment-sync/ping-url)
    sudo cp ibp-shipment-sync.service ibp-shipment-sync.timer /etc/systemd/system/
    sudo systemctl daemon-reload

Backfill once, before enabling the timer (about 12 months; takes a few
minutes, mostly paced listing requests):

    ~/.local/bin/ibp-shipment-sync --since 2025-11-01

Then:

    sudo systemctl enable --now ibp-shipment-sync.timer
    sudo systemctl start ibp-shipment-sync.service && journalctl -u ibp-shipment-sync -n 30

Rebuild the links alone (no key, no EasyPost requests), e.g. after fixing
units: `~/.local/bin/ibp-shipment-sync --rebuild-links`.

If the EasyPost key changes, update the key file (and Bitwarden).
