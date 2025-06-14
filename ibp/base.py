"""Initialize the IBP FastAPI application."""

import configparser
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def get_toplevel_path() -> Path:
    """Get project toplevel path."""
    return Path(__file__).parent.parent


def read_server_config():
    """Read configuration file at module load time."""
    toplevel = get_toplevel_path()
    filepath = os.path.join(toplevel, "conf", "server.conf")
    server_config = configparser.ConfigParser()
    server_config.read([filepath])
    return server_config


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
