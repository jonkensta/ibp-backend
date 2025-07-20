"""Concurrency locks for inmate operations."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator, DefaultDict


_LOCKS: DefaultDict[tuple[str, int], asyncio.Lock] = defaultdict(asyncio.Lock)


@asynccontextmanager
async def inmate_lock(jurisdiction: str, inmate_id: int) -> AsyncIterator[None]:
    """Async context manager providing a lock for a specific inmate."""

    lock = _LOCKS[(jurisdiction, inmate_id)]
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()
