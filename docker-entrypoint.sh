#!/bin/sh
# Bootstrap the database schema, then exec the container command.
set -e

uv run python -m ibp.db_bootstrap

exec "$@"
