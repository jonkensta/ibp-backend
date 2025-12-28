"""IBP database methods."""

import urllib.parse

from typing import Any

from sqlalchemy import MetaData

# pylint: disable=no-name-in-module
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .base import config


def map_engine_scheme(scheme: str) -> str:
    """Map generic database scheme to async driver version."""
    scheme_mapping = {
        "postgresql": "postgresql+asyncpg",
        "postgres": "postgresql+asyncpg",
        "sqlite": "sqlite+aiosqlite",
    }

    return scheme_mapping.get(scheme, scheme)


def get_engine_kwargs(scheme: str) -> dict[str, Any]:
    """Get engine kwargs based on database URI scheme."""
    engine_kwargs: dict[str, dict[str, Any]] = {
        "sqlite+aiosqlite": {
            "connect_args": {"check_same_thread": False},
        },
        "postgresql+asyncpg": {
            "pool_pre_ping": True,  # Verify connections before using
            "pool_size": 5,  # Connection pool size
            "max_overflow": 10,  # Max additional connections
        },
    }

    if scheme not in engine_kwargs:
        supported = list(engine_kwargs.keys())
        raise ValueError(
            f"Unsupported database scheme '{scheme}'. Supported: {supported}"
        )

    return engine_kwargs[scheme]


def build_engine():
    """Build an async engine."""
    uri = config.get("database", "uri")
    parsed = urllib.parse.urlparse(uri)
    mapped_scheme = map_engine_scheme(parsed.scheme)
    uri = uri.replace(f"{parsed.scheme}://", f"{mapped_scheme}://", 1)
    engine_kwargs = get_engine_kwargs(mapped_scheme)
    return create_async_engine(uri, **engine_kwargs)


def build_async_sessionmaker():
    """Build an async sessionmaker."""
    return async_sessionmaker(
        autocommit=False, autoflush=False, bind=build_engine(), class_=AsyncSession
    )


async_session = build_async_sessionmaker()


class Base(DeclarativeBase):  # pylint: disable=too-few-public-methods
    """Base for SQLAlchemy models."""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )

    def update_from_kwargs(self, **kwargs):
        """Update a model object from given keyword arguments."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                msg = f"'{self.__class__}' has no attribute named '{key}'"
                raise AttributeError(msg)
