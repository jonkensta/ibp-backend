# About

This repository hosts the Inside Books Project database web interface.

# Installation

```bash
uv venv
source .venv/bin/activate
uv sync
```

# Development

After going through each of the installation steps,
you can run the server in development mode on your local machine by doing the following:

```bash
FLASK_ENV=development python -m ibp
```

By default, this will load the interface on [localhost port 8000](http://localhost:8000).
