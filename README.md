# Inside Books Project Backend

This repository contains the FastAPI backend that powers the Inside Books Project database interface. Inside Books Project is a volunteer-run organization based in Austin that sends free books and educational materials to incarcerated people throughout Texas. Volunteers correspond with individuals in prisons, locate requested books, and ship packages every week.

The backend server makes it easier for volunteers to manage these logistics. It provides REST API endpoints for looking up Texas Department of Criminal Justice inmate data, tracking book requests, storing comments, and managing the list of prison units. The data served here is used by the web interface volunteers rely on when preparing outgoing shipments.

## Installation

Use [`uv`](https://github.com/astral-sh/uv) to create and synchronize a virtual environment:

```bash
uv venv
source .venv/bin/activate
uv sync
```

## Running the server

During development you can launch the application with FastAPI's development server which enables live reloads:

```bash
uv run fastapi dev ibp
```

It listens on [localhost:8000](http://localhost:8000) and, with the sample
config, uses `data.db` in the project directory. Create the schema first with
`uv run python -m ibp.db_bootstrap`.

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
17 digits, UPC-A check digit on the first 12; anything else is a 422). The
12 digits identify only the publisher and price point, shared by many
titles, and the add-on tells them apart, so a code is answered only by an
exact match on the full code. The answer is the ISBN lookup above plus `isbn`
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

## Inmate search

`GET /inmates?query=` searches by name or inmate ID. It first asks the
providers (TDCJ, FBOP) and saves what they return, then answers from the
`inmates` table, so the table serves as a cache when a provider is down.

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
  TDCJ needs the exact last name (the first name may be a prefix), and FBOP
  needs both names exactly and finds nobody by an initial.

Other old matches stay in `inmates`, below fresh ones with the same name. An
ID search never sets its match aside.

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

## Deployment

Production runs on `ibp-server`, a Raspberry Pi 4B, as a Docker Compose
stack defined in [`deploy/pi/`](deploy/pi/README.md):

- `ibp-frontend`: nginx on port 80, serving the React app and proxying
  `/api/` to the backend.
- `ibp-backend`: this app under uvicorn on port 8000, reachable only from the
  frontend container. Its entrypoint runs `python -m ibp.db_bootstrap`, which
  creates the schema on an empty database or applies pending alembic
  migrations, then starts the server.

Both images are built for amd64 and arm64 by GitHub Actions
(`.github/workflows/docker.yml`) and published to GHCR as
`ghcr.io/jonkensta/ibp-backend` and `ghcr.io/jonkensta/ibp-frontend`, tagged
`main`, `sha-<sha>` and `vX.Y.Z`; production pins a release tag via `IBP_TAG`
in `/srv/ibp/.env`. The backend needs no config file there: it uses the
image's `sample.conf` with `DATABASE_URL` pointing at `/data/data.db`, a host
directory bind-mounted into the container. Setup, upgrades, backups and
overriding the config are covered in
[`deploy/pi/README.md`](deploy/pi/README.md).

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

Run the tests with `uv run pytest -q`.
