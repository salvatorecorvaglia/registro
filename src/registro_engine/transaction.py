"""Transaction helpers that surface adapter transactions through the engine."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from registro_core.protocols.adapter import DatabaseAdapter, Transaction


@asynccontextmanager
async def transaction(adapter: DatabaseAdapter) -> AsyncIterator[Transaction]:
    """Open a transaction, commit on success, roll back on exception."""
    tx = await adapter.begin()
    # ``__aexit__`` commits on a clean exit and rolls back when it is handed an
    # exception, so the context manager is the whole implementation.
    async with tx as active:
        yield active
