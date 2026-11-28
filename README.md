# Inside Books Project Backend

This repository contains the FastAPI backend of the Inside Books Project
database. Inside Books Project is a volunteer-run organization based in Austin
that sends free books and educational materials to incarcerated people
throughout Texas. The backend serves the volunteers' web app
([`frontend`](https://github.com/insidebooksproject/frontend)), the shipping
apps (`shippy` and `shippy-gui`, both in
[`shippy`](https://github.com/insidebooksproject/shippy)) and the Book Portal
extension: inmate lookups
from TDCJ and the Federal Bureau of Prisons, book requests and comments,
prison units, ISBN and UPC lookups, the packing station's package items, and
EasyPost shipment tracking.

## Development

With [`uv`](https://github.com/astral-sh/uv):

```bash
uv sync                             # .venv with the dev tools
uv run python -m ibp.db_bootstrap   # create the schema
uv run fastapi dev ibp              # localhost:8000, reloads on changes
uv run pytest -q
```

With the sample config the database is `data.db` in the current directory.
CI runs the tests (`.github/workflows/tests.yml`) and `black --check`,
`pylint` and `mypy` on `ibp` (`.github/workflows/lint.yml`) on every push and
pull request.

## Configuration

The server reads `sample.conf`, the committed template whose values are the
production defaults, and then reads the first of these over it, if any:

1. the path in the `CONF` environment variable (relative paths are resolved
   against the project directory). If `CONF` is set and the file does not
   exist, startup fails with an error naming the path.
2. `server.conf` in the project directory, if it exists (gitignored).

The override only needs the sections and options it changes; anything it
leaves out keeps its `sample.conf` value, so to turn off an inherited one,
set it empty (for example `anthropic_api_key =` under `[books]`, which would
otherwise come from `ANTHROPIC_API_KEY`). Startup fails if no file exists.
Values may use `${VAR}` (required environment variable) or
`${VAR:-default}`; for example the database URI is
`${DATABASE_URL:-sqlite+aiosqlite:///data.db}`.

`[server] timezone` (an IANA name, default `America/Chicago`) decides what
"today" is, e.g. for the future-postmark warning; startup fails on an unknown
name. It does not depend on the container's `TZ`. `GET /server-config` reports
it read-only (`{"timezone": "America/Chicago"}`) for the frontend's calendar.

The `[books]` section configures the ISBN lookup (`GET /books/{isbn}`): it
queries ISBNdb, Open Library, Google Books and the Library of Congress at
once, has Claude merge their answers into one title and author, and caches
the result in the `books` table (`ibp/books.py`). The keys come from
`ISBNDB_KEY`, `GOOGLE_BOOKS_KEY` and `ANTHROPIC_API_KEY`; all are optional.
Without the Anthropic key, or when the Claude call fails, the first provider
that answered is used (and, if Claude is configured, not cached, so the next
lookup tries the merge again). Claude spend is capped by the Anthropic
workspace's spend limit.

Some mass-market paperbacks carry a retail UPC-A barcode instead of the ISBN
one; `GET /books/upc/{code}` handles those from the `upcs` table, a local
UPC -> ISBN map that volunteers fill (`ibp/upcs.py`; no outside service is
asked). `code` is the 12-digit UPC plus any 2- or 5-digit add-on (12, 14 or
17 digits, UPC-A check digit on the first 12; anything else is a 422), with
or without the leading 0 some scanners send (the UPC as an EAN-13), which is
dropped (`upcs.normalize_code`). The 12 digits identify only the publisher
and price point, shared by many titles, and the add-on tells them apart, so
a code is answered only by an exact match on the full code. The answer is the ISBN lookup above plus `isbn`
and `upc_source` (`volunteer`). A code with no known ISBN is a 404
`{"detail": "No ISBN known for this UPC.", "reason": "no_isbn"}`; one whose
ISBN no provider knows is a 404 `{"detail": "Book not found.", "reason":
"no_book", "isbn": "<ISBN-13>"}`. `PUT /books/upc/{code}` with `{"isbn":
"..."}` records the ISBN a volunteer scanned for the code (204); it needs the
add-on (a bare 12-digit UPC would map many titles to one ISBN, so it is a
422) and is a 409 if the code already maps to a different ISBN. The Book
Portal extension sends it when an unknown UPC with an add-on is followed by
an ISBN scan. Rows keyed by a bare 12-digit UPC with a NULL `isbn` are misses
cached by UPCitemdb and Barcode Lookup, which v1.0.10 to v1.0.12 asked; they
answer nothing.

The packing station fills both tables as books are typed in. Adding a
package item (`POST /requests/{code}/items`) with `"remember_book": true`
and an ISBN saves its title and author in `books` (source `manual`) when no
row has that ISBN yet; it never overwrites one (correcting a cached title is
the edit's `update_cache`, "Fix this title for all future scans"). An
optional `upc` with its add-on, together with an ISBN, is saved in `upcs`
when the code has no row yet; one already mapped elsewhere is kept, without
an error, and a bare 12-digit UPC is never saved. Both writes share the
item's transaction, and the item saves even if they can't happen. The
response's `remembered` lists what was saved: `"isbn"`, `"upc"`, both or
neither.

## Inmate search

`GET /inmates?query=` searches by name or inmate ID. It first asks the
providers (TDCJ, FBOP) and saves what they return, then answers from the
`inmates` table, so the table serves as a cache when a provider is down.

Last names are compared without their spaces, so "juan de la cruz" finds a
Texas DELACRUZ (TDCJ stores such names run together) and a federal DE LA
CRUZ. TDCJ refuses a name with characters its search form does not take, so
it is sent the last name without spaces and the first name's leading letters
(it matches first names by prefix). A last name it would still refuse, such
as one with a digit, is not sent, and that search treats TDCJ's answer as
capped.

A name search sets aside matches that are stale: an inmate the provider was
just asked for and did not return, most likely because they were released.
Those come back as `stale_inmates`, not in `inmates`. A match is stale only if
all of these hold:

- its data is older than `[search] stale_after_days` (30 days), or was never
  fetched;
- its provider neither failed nor capped its answer for this search. TDCJ
  lists 50 results a page and is read up to 10 pages; FBOP sends at most 100
  records, with no paging. An error that names no provider means nothing is
  stale;
- the provider's own search would have returned it for one of the query's
  (first, last) orderings. The local search matches both names by prefix, but
  TDCJ needs the exact last name, spaces aside (the first name may be a
  prefix), and FBOP needs both names exactly and finds nobody by an initial.

Other old matches stay in `inmates`, below fresh ones with the same name. An
ID search never sets its match aside.

### Race and sex

An inmate's `race` and `sex` are stored as the same words for either
provider: race `White`, `Black`, `Hispanic`, `Asian`, `American Indian`,
`Other` or `Unknown`; sex `Male`, `Female` or `Unknown`. TDCJ reports one-letter
codes and FBOP reports words (it has no Hispanic category), so each provider
normalizes its values when it builds a result (`ibp/providers/normalize.py`),
and alembic revision `d2a6f8c3e1b7` rewrote the codes already stored for Texas
inmates. `Unknown` means the provider said so; no value at all is null. A value
not in the tables is stored unchanged and logged as a warning.

## Request addresses

`GET /requests/{code}/address` (and `GET /requests/legacy/{legacy_id}/address`,
plus the deprecated aliases shippy still calls) returns the mailing address
for a request's package. It records no lookup, but if the inmate's data is
older than `[warnings] inmates_cache_ttl` (24 hours), it first re-queries the
inmate's own provider (TDCJ or FBOP, `[providers] timeout`) and updates the
inmate, so a transfer is picked up. `inmate_status` reports the outcome:

- `current`: the data was fresh, or the provider was re-queried and lists the
  inmate (`datetime_fetched` is then the new fetch time).
- `not_listed`: TDCJ answered without error but has no such inmate (it drops
  released inmates). Texas only: an empty answer from another provider is
  `unverified`. The stored address is returned and the inmate left unchanged;
  shippy then buys no postage.
- `unverified`: the provider query failed or timed out; the stored address is
  returned.

An inmate without a unit is a 409, including one the provider now lists at a
unit missing from the `units` table (the detail names that unit).

## Pronouns

A volunteer can record the pronouns a person stated in a letter:
`PUT /inmates/{jurisdiction}/{inmate_id}/pronouns` with `pronouns` (1 to 30
letters, spaces and `/,.'-`, so it can't hold notes) and `set_by` (the
volunteer's initials), and `POST .../pronouns/clear` with `set_by` to remove
them. They are stored in `inmates.pronouns`, with who set them and when; all
three are empty when none were stated, and there is no history.

- Recorded only when the person stated them: never inferred, defaulted or
  backfilled, and never derived from the provider's `sex`.
- Shown only in the inmate view (`GET /inmates/{jurisdiction}/{inmate_id}`
  and its lookups `POST`) and, without who or when, with the inmate at the
  packing station (`GET /requests/{code}`).
- Never on a label or slip and never in the shipping address
  (`GET /requests/{code}/address`), since the label goes out on the package
  and pronouns next to a name could put the person at risk. Not in search
  results or the legacy inmate routes the Book Portal extension calls.
- Kept when a provider refresh updates the inmate, and never logged.

## Book suggestions

`GET /book-suggestions?title=...&author=...` offers a better spelling of a
title and author a volunteer typed by hand (`ibp/book_search.py`). Both are
cleaned and must be 3 to 100 characters, as the portal requires (else 422).
The answer is always a 200: `{"suggestion": {"title", "author", "confidence",
"notes"}}` or `{"suggestion": null}`, which is also the answer when a
provider or Claude fails, when `[books] suggestions` is off, without
`ANTHROPIC_API_KEY`, and for IBP's own items (the Resource Guide and the
Self-Care zine). Only the typed title and author leave the server: to the
book providers of the ISBN lookup (Google and ISBNdb only with their keys)
and to Anthropic. The suggestion carries no ISBN and nothing is stored but an
in-process cache (30 days for a suggestion, 7 for none; failures aren't
cached).

A suggestion is offered only when Claude says one candidate is the same book
with high confidence *and* a fixed rule agrees: the title and author are a
candidate's; the main title (before any subtitle) or author differs from what
was typed by more than case, punctuation, articles, diacritics, name order or
initials; each typed main-title word is within 1 edit of the suggested one (2
for words over 5 letters, none for a word with a digit), with no word added
and a `difflib` ratio of at least 0.80; the surname is within 2 edits; the
typed text is not itself a listed book; and no other book scores within 0.05
of the chosen one.

## Shipment tracking

The database records every EasyPost postage label and its USPS tracking
events, so how long packages take to arrive can be worked out later.
`python -m ibp.shipment_sync` fills it from EasyPost (GET requests only), daily
on ibp-server; installing it and the one-time backfill (`--since YYYY-MM-DD`)
are in [`scripts/shipment-sync/`](scripts/shipment-sync/README.md).

- `shipments`: one row per label, keyed by its EasyPost shipment id: tracking
  code, tracker id, purchase time (UTC), weight (oz), postage (cents),
  service, the `reference` if it is one of the tags below, `kind`
  (`individual`, `bulk` or `other`), the unit it went to when that can be
  told (see below), the request its tag names, the 8-digit inmate number
  from the addressee line, refund status, current tracker status,
  `ever_scanned` (USPS was seen handling it: set by any status or event not
  `unknown`, `pre_transit`, `error` or `cancelled`, and never cleared, so a
  later status or a refund request doesn't undo a scan) and when a sync last
  changed it. No recipient name or street address is stored.
- `tracking_events`: every event as EasyPost lists it (time in UTC, the
  carrier's local time, status, detail, message, city, state, ZIP), returns,
  refusals and repeated deliveries included. A re-sync adds only events not
  stored yet; an exact repeat within a tracker is kept as the next
  `occurrence`.
- `package_shipments`: links a request to each label it could have shipped
  under, with a `basis`. Derived and rebuilt from scratch after every sync, or
  alone with `--rebuild-links`.

A label's unit comes from its tag, else from its address
(`ibp/unit_places.py`, `UnitMatcher` in `ibp/unit_matching.py`). Two kinds
of evidence are gathered apart. By name: an address line (street1, street2
or company, letters and digits only, any case) that is a unit's key name
(the provider's code, e.g. `PLANE JAIL`) or its display name (the first of
the unit's street1 and street2 that is plainly a facility's name: it has a
word such as Unit, Jail, Camp, Center or FCI, and isn't a street number, a
P.O. box or a street such as `FM 686`, `Prison Road` or `IH 45`; a unit
with no such line has no display name), whatever the ZIP. By address,
with the ZIP: a P.O. box number confirms the units with that box, and a
street line is compared with the street line of each unit at its house
number. The same street, allowing USPS's respelling (`264 FM 3478` /
`264 FM 3478 RD`, `1916 North Hwy. 36 Bypass` / `1916 N HIGHWAY 36 BYP`),
confirms the unit; a street that can't be shown the same (a renamed
street, another suffix: `8602 Peach Street` / `8602 PEACH AVE`) confirms
nothing, but is no evidence of another destination either. Only a
well-formed ZIP or ZIP+4 counts (`77-000` or an empty ZIP is none), on both
sides. A name units in several places have is narrowed by the label's ZIP;
if that leaves no place or several, the label has no unit. A name stands
unless the address gives positive evidence for another place: the label's
street (or P.O. box) is the same as another place's and not the named
unit's, or its house number and ZIP are another place's and none of the
named unit's. Then the evidence conflicts: the label has no unit, and an
untagged one is `other` (below). Several units at one address are told
apart by name. Without a name, the confirmed units (or, at a number and
ZIP of one place only, the undecided ones) must be of one place
(`UnitMatcher.locate`).

Units with the same jurisdiction, display name and 5-digit ZIP that also
share a delivery line are one place (one facility under several provider
codes): the same P.O. box number, or the same house number and street once
spelling is normalised (case, punctuation, `S`/`South`, `St`/`Street`,
`Hwy`/`Highway`, `First`/`1st`, a trailing `#1` and the like). Lines that
can't be shown the same (`100 Main Street`, `100 Oak Lane`) are different
places; units with no delivery line are never merged, and units at one
street address with different display names are not one place. A
label to one place stores its representative, the alphabetically first
unit name. Stored labels are matched again only when read again: the daily
run relists 35 days, and `--since YYYY-MM-DD` reads every label back to that
date (see [Re-evaluating every label's
unit](scripts/shipment-sync/README.md#re-evaluating-every-labels-unit)).

A label's EasyPost `reference` carries a tag from the shipping apps:
`ibp1:req:<request code>` (an individual package, e.g.
`ibp1:req:TEX-00012345-2`), `ibp1:unit:<jurisdiction>:<unit name>` (a bulk
box, e.g. `ibp1:unit:Texas:BETO`) or `ibp1:manual` (a manually addressed
label). Anything else, including the empty reference on older labels, is no
tag. A label without a tag is `other` if its destination evidence
conflicts (above), since its addressee's number can't be trusted to be an
inmate at either place; else `individual` if its addressee line ends in an
inmate number, `bulk` if it is addressed to the unit mailroom line
(Settings), and `other` otherwise. Only `individual` labels get
`inmate_number` links. A later listing with clean evidence restores the
kind.

Links (`ibp/shipment_links.py`), none for a label refunded (or with a refund
requested) that USPS never scanned, since it was never mailed:

- `reference`: a tagged label links to the request its tag names.
- `inmate_number`: an untagged individual label links to that inmate's latest
  Filled request processed on or up to 60 days before its purchase date
  (local). The destination unit's jurisdiction tells Texas and Federal
  numbers apart; with no destination, a number both have links to neither.
- `unit_session`: all mailed bulk boxes to one place on one local date are a
  session; a box counts only once USPS has ever scanned it (`ever_scanned`,
  or its current status or events), or while it is under 7 days old (time
  for its first scan). A place is a unit, or the units that
  are one place (above): a box stored under any of them serves the inmates
  under every one. A Filled request with no label of its own links to every
  box of the first session to its unit's place on a date after its
  processed date (a request processed on a session's date waits for the
  next one), if within 28 days. A request's unit is the one its inmate was
  at when it was filled (`requests.unit_name`, recorded since that column
  was added), so a transfer afterwards doesn't move it; one filled while the
  inmate's unit was unknown recorded none and links to no box. An older
  request (`unit_recorded` false) uses its inmate's unit as it is now. The
  unit's shipping method is not consulted: boxes do go to units marked
  Individual.

`GET /requests/{code}/shipments` lists a request's linked labels with their
tracking code, purchase time, kind, basis, tracker status and events.

### Postmark to delivery

`GET /metrics/delivery-latency` (`ibp/delivery_latency.py`) gives, for the
Filled requests of each postmark month (`YYYY-MM`), how long they took to
arrive. `method` is `all` (default), `individual` (links with basis
`reference` or `inmate_number`) or `bulk` (`unit_session`). `start_date`,
`end_date` and the `date_range` preset (`6months` or `12months`, ending
today; `all`) filter the postmark, as for `/metrics/requests`: a given
`start_date` or `end_date` wins for its bound, the preset only fills a
bound not given, and `filters_applied` reports the bounds used. With no
start to the range (none given, and none from the preset), the months before the
first one with a linked request (of any method) are left out, so the series
starts where shipment data does rather than decades back; later months stay
even with no links, and with nothing linked at all the list is empty. A
range with a start lists every month in it. The definitions:

- A label is delivered on the local date (`[server] timezone`) of its
  earliest `delivered` event, unless a `return_to_sender` event came at or
  before it: then it is returned (that "delivery" is the box arriving back
  with us), as is a label with a `return_to_sender` event and no delivery.
- A request's delivery is over its linked labels (of the method) that were
  delivered: `earliest` and `latest` are the first and last delivery dates,
  equal for one label (a bulk request links to every box of its session).
  With none delivered, it is returned if every label was, else not
  delivered yet.
- Latency is the delivery date minus `date_postmarked`, in days, for each
  bound. A request with either bound under 0 or over 365 days (a bad
  postmark) is left out of the statistics and counted as `excluded`.
- Stages: handling (postmark to `date_processed`), waiting to ship
  (processed to the local purchase date of the earliest linked label) and
  transit (that purchase to delivery, per bound). They add up to the
  latency.

Each month has `filled` (every Filled request), `linked` (with a link of
the method), `delivered` (in the statistics), `returned` and `excluded`;
the rest of `linked` are still in the mail. Unlinked and undelivered
requests are only counted, never taken as zero days. For the delivered
ones, `earliest` and `latest` give the 25th percentile, median and 75th
percentile of latency (linear interpolation) and the median transit, and
`median_handling` and `median_waiting` the other stages; all are null when
none was delivered.

Recent months are `provisional`. A letter is not in the database until it
is processed, so a month whose letters are still being answered holds only
its fastest ones, and its figures read low (coverage cannot show this,
since the unanswered letters are missing from `filled` too). The response's
`settle_days` is the 90th percentile of latency (latest bound) over the
delivered requests, every method counted, of the months in the response
that ended more than 120 days before today (in `[server] timezone`),
rounded up; 90 when those months have fewer than 50. A month that ended
fewer than `settle_days` days ago is `provisional`. For example:

```json
{
  "data": [{
    "month": "2026-02",
    "filled": 530, "linked": 431, "delivered": 412, "returned": 3, "excluded": 2,
    "earliest": {"p25": 11.0, "median": 14.0, "p75": 18.0, "median_transit": 4.0},
    "latest": {"p25": 12.0, "median": 15.0, "p75": 19.0, "median_transit": 5.0},
    "median_handling": 7.0,
    "median_waiting": 2.0,
    "provisional": false
  }],
  "settle_days": 98,
  "filters_applied": {"method": "all", "date_range": "12months", ...}
}
```

The metric reads the stored links, so after an upgrade that changes the
link rules, rebuild them (`--rebuild-links`, see
[`deploy/pi/README.md`](deploy/pi/README.md#upgrading)) before trusting it.
It is read in one SQL statement, so a sync committing meanwhile shows up
entirely or not at all; unlinked requests are only counted, in SQL.

## Deployment

Production runs on `ibp-server`, a Raspberry Pi 4B, as a Docker Compose
stack of two containers: `ibp-frontend` (nginx, serving the React app and
proxying `/api/` to the backend) and `ibp-backend` (this app under uvicorn).
The backend image's entrypoint runs `python -m ibp.db_bootstrap`, which
creates the schema on an empty database or applies pending alembic
migrations, then starts the server. The images, setup, upgrades, rollback
and configuration are in [`deploy/pi/README.md`](deploy/pi/README.md).

Every SQLite connection (the server, the shipment sync, the bootstrap and
alembic) uses WAL and a 10 s busy timeout (`ibp/db.py`), since the server
and a sync's one-off container write the same file. While the database is
open, `data.db-wal` and `data.db-shm` sit beside it and are part of it:
copy it with `sqlite3 .backup`, never with `cp` alone.

## Migrating from the legacy database

`scripts/migrate.py` imports the legacy Flask app's SQLite database into a new,
empty database (SQLite or PostgreSQL, selected via `DATABASE_URL`). It is a
single step: the script creates the schema, migrates the data, verifies row
counts, and stamps the alembic revision, so the result is ready for the app.

```bash
DATABASE_URL=sqlite+aiosqlite:///path/to/new.db \
    uv run --group dev scripts/migrate.py path/to/legacy.db --archive-dir path/to/archive
```

- Units, inmates, requests, comments and lookups are migrated. Legacy naive
  datetimes (US Central local time) are converted to UTC; date-only columns
  are copied unchanged.
- Shipments, users, credentials and alerts are not migrated, nor are comments,
  lookups and requests whose inmate is missing. With `--archive-dir`, the
  shipments table, the request-to-shipment links and those orphaned rows are
  written there as CSV files.
- The script refuses to write into a database that already contains data. It
  prints a summary of migrated rows, dropped orphans and archive files.

Running `python -m ibp.db_bootstrap` afterwards (as the container entrypoint
does) is harmless; it finds the stamped schema and has nothing to do.
