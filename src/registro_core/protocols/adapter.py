"""Database adapter protocol — the seam every database plugs into."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from registro_core.models.connection import Dsn
    from registro_core.models.query import QueryHandle, QueryMetadata
    from registro_core.models.result import ResultBatch
    from registro_core.models.schema import (
        ForeignKey,
        Index,
        SchemaSnapshot,
        TableColumn,
    )


@runtime_checkable
class Transaction(Protocol):
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
    async def __aenter__(self) -> Transaction: ...
    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...


@runtime_checkable
class DatabaseAdapter(Protocol):
    """Protocol for an async database adapter.

    Implementations live in ``registro_adapters`` and are looked up by URL
    scheme via the adapter registry.
    """

    name: ClassVar[str]
    schemes: ClassVar[tuple[str, ...]]

    @property
    def connected(self) -> bool: ...

    async def connect(self, dsn: Dsn) -> None: ...
    async def close(self) -> None: ...
    async def ping(self) -> bool: ...

    async def __aenter__(self) -> DatabaseAdapter: ...
    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> QueryHandle:
        """Start *sql* and return a handle to stream from.

        .. warning:: ``params`` is **not** portable across adapters. Each one
           binds using its driver's native paramstyle, and the shape of the
           mapping differs accordingly:

           * ``postgres`` — keys are stringified 1-based indices matching
             ``$1``, ``$2``: ``{"1": value}``.
           * ``mysql`` — keys are identifiers matching ``%(name)s``:
             ``{"name": value}``.
           * ``duckdb`` — keys are identifiers matching ``$name``.
           * ``sqlite`` — whatever :mod:`sqlite3` accepts, i.e. named
             (``:name``) binding via a mapping.

           Portable SQL therefore cannot use bound parameters through this
           interface today. Unifying the styles would be a breaking change to
           every caller and adapter, so it is documented rather than papered
           over.
        """
        ...

    async def stream(
        self, handle: QueryHandle, *, batch_size: int = 1000
    ) -> AsyncIterator[ResultBatch]: ...
    async def metadata(self, handle: QueryHandle) -> QueryMetadata: ...
    async def cancel(self, handle: QueryHandle) -> None: ...
    async def reset(self) -> None: ...

    async def begin(self) -> Transaction: ...

    async def introspect(self, *, lazy: bool = False) -> SchemaSnapshot: ...

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]: ...
    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]: ...
    async def fetch_indexes(self, schema: str, name: str) -> list[Index]: ...
