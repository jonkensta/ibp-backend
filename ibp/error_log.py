"""The shared error log format: the backend's own ``backend.jsonl`` and the
browser reports' ``client-errors.jsonl``.

Both files hold one JSON object per line, the event format every IBP app
writes (see ``docs/error-reporting.md`` in the meta repo, and
``ibp_printing.log.JsonFormatter``, whose fields these follow)::

    {"ts": "2026-11-26T14:03:12.481-06:00", "level": "ERROR",
     "app": "backend", "version": "v1.0.22", "logger": "ibp.api",
     "host": "ibp-server", "msg": "...", "exc": "Traceback ...", "data": {}}

They are written to the log directory, ``[logging] log_dir`` (the
``IBP_LOG_DIR`` environment variable; ``/logs`` in the image, mounted from
``/srv/ibp/logs`` on the Pi). Each file rotates at 5 MiB and keeps 4 old
copies. If the directory can't be written, the backend runs without the
files and says so once on stderr.
"""

import datetime
import json
import logging
import os
import socket
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

APP = "backend"
BACKEND_LOG_NAME = "backend.jsonl"
CLIENT_LOG_NAME = "client-errors.jsonl"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 4
DEFAULT_LOG_DIR = "logs"

logger = logging.getLogger(__name__)


def get_version() -> str:
    """The running release: ``IBP_VERSION``, set when the image is built
    (``vX.Y.Z`` for a release, ``sha-<commit>`` otherwise), or ``dev``."""
    return os.environ.get("IBP_VERSION", "").strip() or "dev"


def get_host() -> str:
    """The name the backend's own events carry: ``IBP_LOG_HOST`` (the Pi's
    compose file sets ``ibp-server``), else the container's host name."""
    return os.environ.get("IBP_LOG_HOST", "").strip() or socket.gethostname()


def get_log_dir() -> Path:
    """The configured log directory.

    A relative path is resolved against the directory of the config file
    (the project toplevel for ``sample.conf``), as ``[logging] logfile`` is.
    """
    # pylint: disable=import-outside-toplevel,cyclic-import
    from .base import config, get_toplevel_path

    value = config.get("logging", "log_dir", fallback=DEFAULT_LOG_DIR).strip()
    path = Path(value or DEFAULT_LOG_DIR).expanduser()
    if not path.is_absolute():
        base_dir = config.path.parent if config.path else get_toplevel_path()
        path = base_dir / path
    return path


def timestamp(created: float) -> str:
    """Local time with its UTC offset: ``2026-11-26T14:03:12.481-06:00``."""
    moment = datetime.datetime.fromtimestamp(created).astimezone()
    return moment.isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    """One event per record, in the shared format, for the backend's own log.

    A record's ``data`` (``extra={"data": {...}}``) is copied when it is a
    dict; ``exc`` is the formatted traceback.
    """

    def __init__(self, app: str = APP, version: str | None = None, host: str = ""):
        super().__init__()
        self.app = app
        self.version = version or get_version()
        self.host = host or get_host()

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": timestamp(record.created),
            "level": record.levelname,
            "app": self.app,
            "version": self.version,
            "logger": record.name,
            "host": self.host,
            "pid": record.process,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict) and data:
            payload["data"] = data
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=repr, ensure_ascii=False)


class EventFormatter(logging.Formatter):
    """Writes a record's ``event`` dict as it is (the client reports)."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(getattr(record, "event"), default=repr, ensure_ascii=False)


def open_log_file(name: str) -> RotatingFileHandler:
    """A size-rotated handler for ``name`` in the log directory.

    Raises ``OSError`` if the directory can't be made or the file opened.
    """
    log_dir = get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(
        log_dir / name,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )


def unavailable_message(name: str, error: OSError) -> str:
    """The one stderr line saying a log file is not being written."""
    return (
        f"Not writing {name}: the log directory {get_log_dir()} can't be "
        f"written ({error}); set IBP_LOG_DIR or [logging] log_dir to one "
        "that can"
    )


def build_backend_handler() -> tuple[logging.Handler | None, str | None]:
    """The handler writing the backend's WARNING and above to
    ``backend.jsonl``, or ``None`` and why not."""
    try:
        handler = open_log_file(BACKEND_LOG_NAME)
    except OSError as error:
        return None, unavailable_message(BACKEND_LOG_NAME, error)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(JsonFormatter())
    return handler, None


class ClientErrorLog:
    """Appends browser reports to ``client-errors.jsonl``.

    Opened on first use (or by :meth:`open` at startup). When the file
    can't be opened, reports go to stderr instead, so they are at least in
    ``docker compose logs``, and the failure is said once. The handler is
    called directly, not through a logger, so no logging configuration
    (levels, disabled loggers) can drop a report.
    """

    def __init__(self) -> None:
        self._handler: logging.Handler | None = None

    def open(self) -> logging.Handler:
        """Open the file, or fall back to stderr; once."""
        if self._handler is not None:
            return self._handler
        handler: logging.Handler
        try:
            handler = open_log_file(CLIENT_LOG_NAME)
        except OSError as error:
            logger.warning("%s", unavailable_message(CLIENT_LOG_NAME, error))
            handler = logging.StreamHandler()
        handler.setFormatter(EventFormatter())
        self._handler = handler
        return handler

    def close(self) -> None:
        """Close the file; the next write opens it again."""
        if self._handler is not None:
            self._handler.close()
            self._handler = None

    def write(self, event: dict[str, Any]) -> None:
        """Append one event."""
        record = logging.makeLogRecord({"msg": "client error", "event": event})
        self.open().handle(record)


client_error_log = ClientErrorLog()
