"""Schema introspection wrapper with caching + event emission."""

from __future__ import annotations

from typing import TYPE_CHECKING

from registro_core.errors import AdapterError, RegistroError
from registro_core.events import EventBus, SchemaRefreshed
from registro_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from registro_core.models.schema import (
        ForeignKey,
        Index,
        SchemaSnapshot,
        TableColumn,
    )
    from registro_core.protocols.adapter import DatabaseAdapter

logger = get_logger(__name__)


async def _guarded[T](operation: str, pending: Awaitable[T]) -> T:
    """Await an adapter call, translating a raw driver error into ``AdapterError``.

    Adapters run their catalog queries straight through the driver, so a lost
    connection or a permission error on a system view arrives as an asyncpg /
    asyncmy / duckdb / sqlite exception. Callers — the TUI in particular — are
    written against the Registro hierarchy; an unexpected type escaping from a
    background worker took the whole app down.
    """
    try:
        return await pending
    except RegistroError:
        raise
    except Exception as exc:
        raise AdapterError(
            f"schema introspection failed: {exc}",
            context={"operation": operation, "error_type": type(exc).__name__},
        ) from exc


class Introspector:
    """Wraps adapter introspection with an in-memory cache.

    Cache invalidates on explicit :meth:`refresh` or :meth:`invalidate`.
    """

    def __init__(self, adapter: DatabaseAdapter, bus: EventBus) -> None:
        self._adapter = adapter
        self._bus = bus
        self._cache: SchemaSnapshot | None = None
        # Whether the cached snapshot was built lazily (objects only, no
        # columns/indexes/foreign keys). A lazy snapshot cannot satisfy a
        # caller that asked for a full one.
        self._cache_is_lazy = False

    async def snapshot(self, *, force: bool = False, lazy: bool = False) -> SchemaSnapshot:
        # A lazy cache does not answer a request for a full snapshot: it holds
        # no columns, and returning it would silently hand back an empty schema.
        # The reverse is fine — a full snapshot is a superset of a lazy one.
        stale = self._cache is not None and self._cache_is_lazy and not lazy
        if self._cache is None or force or stale:
            self._cache = await _guarded("introspect", self._adapter.introspect(lazy=lazy))
            self._cache_is_lazy = lazy
            self._bus.publish(SchemaRefreshed(connection_id=self._cache.connection_id))
            logger.debug(
                "schema_refreshed",
                connection_id=self._cache.connection_id,
                objects=self._cache.object_count,
                lazy=lazy,
            )
        return self._cache

    async def refresh(self, *, lazy: bool = False) -> SchemaSnapshot:
        return await self.snapshot(force=True, lazy=lazy)

    def invalidate(self) -> None:
        self._cache = None
        self._cache_is_lazy = False

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        return await _guarded("fetch_columns", self._adapter.fetch_columns(schema, name))

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        return await _guarded("fetch_foreign_keys", self._adapter.fetch_foreign_keys(schema, name))

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        return await _guarded("fetch_indexes", self._adapter.fetch_indexes(schema, name))

    def update_object(
        self,
        schema: str,
        name: str,
        columns: list[TableColumn],
        foreign_keys: list[ForeignKey],
        indexes: list[Index],
    ) -> None:
        """Update cached SchemaObject metadata with lazily loaded fields."""
        if self._cache is None or schema not in self._cache.schemas:
            return
        new_objects = list(self._cache.schemas[schema])
        updated = False
        for i, obj in enumerate(new_objects):
            if obj.name == name:
                new_objects[i] = obj.model_copy(
                    update={
                        "columns": columns,
                        "foreign_keys": foreign_keys,
                        "indexes": indexes,
                    }
                )
                updated = True
                break
        if updated:
            new_schemas = dict(self._cache.schemas)
            new_schemas[schema] = new_objects
            self._cache = self._cache.model_copy(update={"schemas": new_schemas})
