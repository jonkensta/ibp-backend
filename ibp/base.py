"""Initialize the IBP FastAPI application."""

import configparser
import logging
import os
import re
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

    def get(self, section, option, **kwargs):
        """Get config value with environment variable interpolation."""
        value = super().get(section, option, **kwargs)
        return self._interpolate_env(value)

    def _interpolate_env(self, value):
        """Replace ${VAR} or ${VAR:-default} with environment values."""
        def replacer(match):
            var_expr = match.group(1)

            # Handle ${VAR:-default} syntax
            if ':-' in var_expr:
                var_name, default = var_expr.split(':-', 1)
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
        return re.sub(r'\$\{([^}]+)\}', replacer, value)


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
        return server_config

    # Search default locations in order of precedence
    for config_file in ["server.conf", "sample.conf"]:
        filepath = os.path.join(toplevel, config_file)
        if os.path.exists(filepath):
            server_config = EnvInterpolatingConfigParser()
            server_config.read([filepath])
            return server_config

    # No config file found
    raise FileNotFoundError(
        "No configuration file found. "
        "Set CONF environment variable or create server.conf/sample.conf"
    )


config = read_server_config()

app = FastAPI(
    title="Inside Books Project API",
    description="IBP API for managing inmate requests.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def build_log_handlers():
    """Build log handlers."""
    format_ = config.get("logging", "format", raw=True)
    formatter = logging.Formatter(format_)

    handler = RotatingFileHandler(
        config.get("logging", "logfile"),
        maxBytes=config.getint("logging", "rotation_size"),
    )
    handler.setFormatter(formatter)
    yield handler


def configure_root_logger(handlers):
    """Configure the root logger."""
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    level = logging.getLevelName(config.get("logging", "level"))
    root_logger.setLevel(level)

    for handler in handlers:
        root_logger.addHandler(handler)


def configure_external_loggers(handlers):
    """Configure external loggers."""
    logger_names = ["asyncio", "uvicorn", "sqlalchemy.engine"]
    loggers = (logging.getLogger(name) for name in logger_names)
    for logger in loggers:
        logger.setLevel(logging.ERROR)
        for handler in handlers:
            logger.addHandler(handler)
