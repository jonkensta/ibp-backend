"""Concurrency locks for inmate operations."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator
from weakref import WeakValueDictionary


_LOCKS: WeakValueDictionary[tuple[str, int], asyncio.Lock] = WeakValueDictionary()


@asynccontextmanager
async def inmate_lock(jurisdiction: str, inmate_id: int) -> AsyncIterator[None]:
    """Async context manager providing a lock for a specific inmate."""

    key = (jurisdiction, inmate_id)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()
