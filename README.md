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

For a production build use the regular `run` command:

```bash
uv run fastapi run
```

Both commands start the application on [localhost:8000](http://localhost:8000) by default. Configuration such as database location and logging options can be adjusted in `conf/server.conf`.

## Deployment

Production runs natively on a Raspberry Pi behind nginx, without Docker. The
production config template, the systemd unit, the nginx site and the
step-by-step install, cutover and rollback procedure are in
[`deploy/`](deploy/README.md).

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

Run the tests with `uv run --group dev pytest`.
