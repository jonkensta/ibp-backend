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

The server reads exactly one INI config file, found in this order:

1. the path in the `CONF` environment variable (relative paths are resolved
   against the project directory). If `CONF` is set and the file does not
   exist, startup fails with an error naming the path.
2. `server.conf` in the project directory, if it exists (gitignored; copy
   `sample.conf` to start one).
3. `sample.conf`, the committed template, whose defaults are the production
   values.

The file is not merged with `sample.conf`, so a `server.conf` must contain
every section. Values may use `${VAR}` (required environment variable) or
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
one; `GET /books/upc/{code}` handles those (`ibp/upcs.py`). `code` is the
12-digit UPC plus any 2- or 5-digit add-on (12, 14 or 17 digits, UPC-A check
digit on the first 12; anything else is a 422). A volunteer's mapping of the full
code (with its add-on) wins; else the 12 digits are mapped to an ISBN from
the `upcs` table or by [UPCitemdb](https://www.upcitemdb.com/), which knows
no add-ons, so its answer (hit or miss) is cached under the 12 digits and
shared by every add-on. The answer is the ISBN lookup above plus `isbn` and
`upc_source` (`upcitemdb`, `barcodelookup` or `volunteer`). A UPC with no known ISBN is a 404
`{"detail": "No ISBN known for this UPC.", "reason": "no_isbn"}`, remembered
for 30 days before UPCitemdb is asked again; a UPC whose ISBN no provider
knows is a 404 `{"detail": "Book not found.", "reason": "no_book", "isbn":
"<ISBN-13>"}`. UPCitemdb rate-limited or unreachable is a 503 and is not
remembered. `PUT /books/upc/{code}` with `{"isbn": "..."}` records the ISBN
a volunteer found under the full code (204), overriding whatever the 12
digits map to; it needs the add-on (a bare 12-digit UPC on older paperbacks
may identify only publisher and price, so it is a 422) and is a 409 if the
full code already maps to a different ISBN. Writes are conditional, so a
concurrent request never overwrites an ISBN another one stored. UPCitemdb's free trial endpoint allows 100 lookups a day
per public IP without a key; set `UPCITEMDB_KEY` (`upcitemdb_key`) to use the
paid one. With `BARCODELOOKUP_KEY` (`barcodelookup_key`) set, a UPCitemdb
503 falls back to [Barcode Lookup](https://www.barcodelookup.com/api) (free
plan: 50 lookups a month); a 503 then means both were unavailable.

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
