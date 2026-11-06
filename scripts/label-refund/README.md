# Unused-label refunds

Weekly job on ibp-server that asks EasyPost to refund USPS labels nobody
mailed. EasyPost refunds a USPS label only if USPS never scanned it and the
refund is requested within 30 days of purchase; it never does this on its
own.

- `ibp-label-refund`: lists labels bought 14-29 days ago, refreshes each
  unscanned one's tracker, and requests a refund for those still unscanned
  with no refund requested yet. Python 3 stdlib only.
- `ibp-label-refund.service` / `.timer`: Wednesdays 10:00, `Persistent=true`.
  A weekly run refunds each label at day 14-21; a missed run is caught the
  next week, still inside the 30 days.

Why 14 days: of 4,266 labels used in the 12 months to 2026-11-05, 99.9% had
their first USPS scan within 5 days; one took 27. A label refunded and then
mailed anyway usually still gets delivered, and EasyPost may reverse the
refund.

Safety:

- Dry run unless `IBP_REFUND_MODE=apply` (the unit ships as `dry-run`) or
  `--apply`.
- If more than 15 labels qualify it refunds nothing and exits 2. That many
  unused labels means something is wrong (say, a printer was down and the
  labels went out late); check and refund by hand.
- Only production keys (`EZAK...`), USPS labels, and labels with a tracker.
- A refund request is never retried, since a timed-out POST may have landed.
- Output has shipment ids, tracking codes, dates and amounts, no names.
  Requests are appended to `~/.local/state/ibp-label-refund/refunds.csv`.

Monitoring: pings a Healthchecks check (start, then exit status with the
output) if `~/.config/ibp-label-refund/ping-url` exists. Exit 1 = API error
or a failed refund, 2 = over the limit.

## Install

    install -D -m755 ibp-label-refund ~/.local/bin/ibp-label-refund
    install -d -m700 ~/.config/ibp-label-refund
    # production key, from Bitwarden app.easypost.com field api-key-production
    (umask 077; cat > ~/.config/ibp-label-refund/easypost-api-key)
    # optional: (umask 077; echo https://... > ~/.config/ibp-label-refund/ping-url)
    sudo cp ibp-label-refund.service ibp-label-refund.timer /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now ibp-label-refund.timer
    sudo systemctl start ibp-label-refund.service && journalctl -u ibp-label-refund -n 30

To start refunding, after a couple of dry runs look right:

    sudo sed -i 's/IBP_REFUND_MODE=dry-run/IBP_REFUND_MODE=apply/' /etc/systemd/system/ibp-label-refund.service
    sudo systemctl daemon-reload

If the EasyPost key changes, update the key file (and Bitwarden).
