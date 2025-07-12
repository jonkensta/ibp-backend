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
