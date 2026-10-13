"""Initialize the IBP FastAPI application."""

import configparser
import contextlib
import logging
import os
import re
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


class EnvInterpolatingConfigParser(configparser.ConfigParser):
    """ConfigParser that substitutes ${ENV_VAR} and ${ENV_VAR:-default} syntax.

    Examples:
        ${DATABASE_URL} - reads from DATABASE_URL env var, raises error if not set
        ${DATABASE_URL:-sqlite:///data.db} - reads from env var, uses default if not set
    """

    #: The file this configuration was read from, if any.
    path: Path | None = None

    def get(self, section, option, **kwargs):
        """Get config value with environment variable interpolation."""
        value = super().get(section, option, **kwargs)
        if not isinstance(value, str):  # e.g. a non-string fallback
            return value
        return self._interpolate_env(value)

    def _interpolate_env(self, value):
        """Replace ${VAR} or ${VAR:-default} with environment values."""

        def replacer(match):
            var_expr = match.group(1)

            # Handle ${VAR:-default} syntax
            if ":-" in var_expr:
                var_name, default = var_expr.split(":-", 1)
                return os.getenv(var_name.strip(), default.strip())

            # Handle ${VAR} syntax (required, no default)
            var_name = var_expr.strip()
            env_value = os.getenv(var_name)
            if env_value is None:
                raise ValueError(
                    f"Required environment variable '{var_name}' is not set. "
                    f"Please set it in your environment or .env file."
                )
            return env_value

        # Match ${...} patterns
        return re.sub(r"\$\{([^}]+)\}", replacer, value)


def get_toplevel_path() -> Path:
    """Get project toplevel path."""
    return Path(__file__).parent.parent


def read_server_config():
    """Read configuration file at module load time.

    Config file resolution order:
    1. CONF environment variable (if set) - explicit path override
    2. server.conf (if exists) - local/production config
    3. sample.conf (fallback) - template

    Supports environment variable substitution via ${VAR} or ${VAR:-default} syntax.
    """
    toplevel = get_toplevel_path()

    # Check for explicit override via CONF environment variable
    config_file = os.getenv("CONF")
    if config_file:
        filepath = os.path.join(toplevel, config_file)
        server_config = EnvInterpolatingConfigParser()
        server_config.read([filepath])
        server_config.path = Path(filepath).resolve()
        return server_config

    # Search default locations in order of precedence
    for config_file in ["server.conf", "sample.conf"]:
        filepath = os.path.join(toplevel, config_file)
        if os.path.exists(filepath):
            server_config = EnvInterpolatingConfigParser()
            server_config.read([filepath])
            server_config.path = Path(filepath).resolve()
            return server_config

    # No config file found
    raise FileNotFoundError(
        "No configuration file found. "
        "Set CONF environment variable or create server.conf/sample.conf"
    )


config = read_server_config()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    """Configure logging when the application starts."""
    handlers = list(build_log_handlers())
    configure_root_logger(handlers)
    configure_external_loggers(handlers)
    logging.getLogger(__name__).info("Starting IBP Application")
    yield


app = FastAPI(
    title="Inside Books Project API",
    description="IBP API for managing inmate requests.",
    version="0.1.0",
    root_path="/api",
    lifespan=lifespan,
)


def get_cors_origins() -> list[str]:
    """Return the origins allowed cross-origin access to the API.

    Production serves the frontend and proxies ``/api/`` through the same nginx
    origin, so no CORS is needed and the list is empty by default. A dev setup
    that serves the frontend from another origin can opt in by listing exact
    origins (comma or whitespace separated) under ``[server] cors_origins``.
    """
    value = config.get("server", "cors_origins", fallback="")
    return [origin for origin in re.split(r"[,\s]+", value) if origin]


cors_origins = get_cors_origins()
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5


def get_log_level() -> str:
    """Return the configured root log level name (INFO by default)."""
    return config.get("logging", "level", fallback=DEFAULT_LOG_LEVEL).upper()


def get_logfile_path() -> Path | None:
    """Return the configured log file path, or None to log to stderr.

    A relative path is resolved against the directory of the config file (or
    the project toplevel if no file was read), never the process working
    directory.
    """
    logfile = config.get("logging", "logfile", fallback="").strip()
    if not logfile:
        return None

    path = Path(logfile).expanduser()
    if not path.is_absolute():
        base_dir = config.path.parent if config.path else get_toplevel_path()
        path = base_dir / path
    return path


def build_log_handlers() -> Iterator[logging.Handler]:
    """Build log handlers.

    With no ``logfile`` configured, log to stderr (picked up by journald under
    systemd). Otherwise log to a size-rotated file whose total size is bounded
    by ``max_bytes * (backup_count + 1)``.
    """
    format_ = config.get("logging", "format", raw=True, fallback=DEFAULT_LOG_FORMAT)
    formatter = logging.Formatter(format_)

    path = get_logfile_path()
    handler: logging.Handler
    if path is None:
        handler = logging.StreamHandler()
    else:
        # ``rotation_size`` is the legacy name for ``max_bytes``.
        max_bytes = config.getint(
            "logging",
            "max_bytes",
            fallback=config.getint(
                "logging", "rotation_size", fallback=DEFAULT_LOG_MAX_BYTES
            ),
        )
        backup_count = config.getint(
            "logging", "backup_count", fallback=DEFAULT_LOG_BACKUP_COUNT
        )
        if max_bytes <= 0 or backup_count <= 0:
            raise ValueError(
                "[logging] max_bytes and backup_count must be positive "
                "so the log size stays bounded"
            )
        handler = RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )

    handler.setFormatter(formatter)
    yield handler


def configure_root_logger(handlers):
    """Configure the root logger."""
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(get_log_level())

    for handler in handlers:
        root_logger.addHandler(handler)


def configure_external_loggers(handlers):
    """Quiet noisy external loggers down to errors.

    Loggers that propagate reach the root handlers already. Non-propagating
    ones (uvicorn's) get the file handlers so their errors land in the log
    file; they already write to stderr themselves.
    """
    logger_names = ["asyncio", "uvicorn", "sqlalchemy.engine", "aiosqlite"]
    loggers = (logging.getLogger(name) for name in logger_names)
    for logger in loggers:
        logger.setLevel(logging.ERROR)
        if logger.propagate:
            continue
        for handler in handlers:
            if isinstance(handler, logging.FileHandler) and handler not in (
                logger.handlers
            ):
                logger.addHandler(handler)
