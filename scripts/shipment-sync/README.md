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
  lists the last 35 days, or from a day before the newest stored label if
  that is older, so even a longer outage leaves no gap.

Each run logs one line of counts, e.g.
`shipment sync: 412 labels seen, 11 new, 37 updated, 0 fetched by id, 0 skipped, 160 events added, 388 trackers polled (0 failed), 1420 links built`.
No names or addresses are logged or stored. A listed label not of
EasyPost's shape (an id other than `shp_` and hex digits, a tracker id
other than `trk_` and hex digits, a tracking code other than 8 to 40
letters and digits, or a field of the wrong type) is not stored and is
counted as "skipped", and the run then exits 1. A polled tracker must
answer with its own id and one of EasyPost's statuses, else it is a failed
poll and nothing stored changes. Stored evidence never goes backwards: an
answer without a tracker, tracker status or refund status keeps the stored
one, events are only ever added, and a label once seen scanned stays
`ever_scanned` whatever its later status or refund. Every listing page must have its list of
shipments and a boolean `has_more`; a page saying more follow must have a
last shipment to page on, other than the previous page's; and a month's
window may have at most 500 pages. Anything else fails the run rather than
ending (or never ending) the listing as if it were complete. Only
checked ids reach a log line.

Exit status: 0 ok, 1 an EasyPost or database error (or a tracker it could
not fetch, or a listed label it skipped) or any other failure, 2 no usable
key, 75 another run in progress (the wrapper's lock, below). Failures, retries and
unfetched trackers are logged as one line each with no values: a fixed
operation (`GET shipments`, `GET shipment`, `GET tracker`, never an id or
query string) with an EasyPost error's HTTP status and code (a known code,
else `other`), or a network error's class; a database error's class and
SQLite's error code name (`OperationalError: SQLITE_BUSY`), never its
message; anything else its class and the file and line in `ibp/` that
raised it (`ValueError at ibp/shipment_sync.py:384`); never a traceback.
An unfetched tracker's warning names the label's EasyPost id. Monitoring: pings a Healthchecks check (start,
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

Rebuild the links alone (no key, no EasyPost requests), e.g. after a
release that changes only the link rules:
`~/.local/bin/ibp-shipment-sync --rebuild-links`. That does not match any
label to a unit again.

## Re-evaluating every label's unit

A stored label's unit is matched when it is listed, so after a release that
changes unit matching (`ibp/unit_places.py`, `ibp/unit_matching.py`,
`ibp/label_reading.py`), or after fixing units' names or addresses, every
label must be read again. `--since <date>` lists every label bought from
that date on, then fetches by id each stored label bought since then that
the listing missed (the listing goes by when the EasyPost shipment was
created, which can be before the label was bought; the summary counts them
as "fetched by id"), and only then rebuilds the links. A stored label
counts as read again only once an answer with its own id was read and
stored: an answer with no label, another shipment's id or a shape not
EasyPost's makes the run incomplete, as does any tracker that can't be
fetched. Pages are committed as they go, so a run that fails part-way
leaves some labels matched the new way and some the old: it then exits 1
without rebuilding the links, logging that the re-evaluation is incomplete
and must be re-run with the same `--since`. Keep the daily run from
rebuilding links from that mix meanwhile, and replace the wrapper (the
old one takes no lock) before the backfill, in this order:

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
    # 5. Upgrade the backend (deploy/pi/README.md, Upgrading: backup, new
    #    IBP_TAG, docker compose pull && docker compose up -d).
    # 6. Re-evaluate every label from the first label's date until it exits
    #    0; it is re-run only after an incomplete run (1); any other status
    #    (2 no key, 75 another run) stops the loop to be looked at.
    while true; do ~/.local/bin/ibp-shipment-sync --since 2025-11-01; status=$?; [ "$status" -eq 1 ] || break; echo "not complete; again in 60 s"; sleep 60; done; echo "exit status $status"
    # 7. Start the timer again.
    sudo systemctl start ibp-shipment-sync.timer

Runs never overlap: the wrapper holds an exclusive lock
(`~/.local/state/ibp-shipment-sync/run.lock`) for the whole run. A run that
finds it held prints "another ibp-shipment-sync run is in progress" and
exits 75 without running anything or pinging Healthchecks (the run holding
the lock reports for itself, and the service counts 75 as success). The
one-off container is labelled `ibp.job=shipment-sync`.

If the EasyPost key changes, update the key file (and Bitwarden).
