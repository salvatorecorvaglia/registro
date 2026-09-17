"""Shared introspection orchestration.

Each adapter owns its own catalog queries (they are inherently DB-specific),
but the per-object loop, kind dispatch, and ``SchemaSnapshot`` assembly live
here once. Subclasses normalize ``kind`` to one of ``"table"``, ``"view"``,
``"materialized_view"`` so the base class can dispatch without per-DB casing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from registro_core.models.schema import (
    SchemaSnapshot,
    Table,
    View,
)

if TYPE_CHECKING:
    from registro_core.models.schema import (
        ForeignKey,
        Index,
        SchemaObject,
        TableColumn,
    )


class SchemaIntrospector(ABC):
    """Template-method base that drives the per-object introspection loop."""

    @abstractmethod
    def connection_id(self) -> str: ...

    @abstractmethod
    def database_name(self) -> str | None: ...

    @abstractmethod
    async def list_objects(self) -> list[tuple[str, str, str]]:
        """Return ``(schema, name, kind)`` for every visible table/view.

        ``kind`` must be normalized to ``"table"``, ``"view"``, or
        ``"materialized_view"``.
        """

    @abstractmethod
    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]: ...

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        return []

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        return []

    async def fetch_all_columns(self) -> dict[tuple[str, str], list[TableColumn]] | None:
        return None

    async def fetch_all_foreign_keys(self) -> dict[tuple[str, str], list[ForeignKey]] | None:
        return None

    async def fetch_all_indexes(self) -> dict[tuple[str, str], list[Index]] | None:
        return None

    def build_view(self, schema: str, name: str, kind: str, columns: list[TableColumn]) -> View:
        from typing import Literal, cast  # noqa: PLC0415

        return View(
            name=name,
            schema_name=schema,
            columns=columns,
            kind=cast('Literal["view", "materialized_view"]', kind),
        )

    async def build(self, *, lazy: bool = False) -> SchemaSnapshot:
        schemas: dict[str, list[SchemaObject]] = {}
        objects = await self.list_objects()

        all_cols = await self.fetch_all_columns() if not lazy else None
        all_fks = await self.fetch_all_foreign_keys() if not lazy else None
        all_indexes = await self.fetch_all_indexes() if not lazy else None

        for schema_name, name, kind in objects:
            obj: SchemaObject
            if lazy:
                cols = []
                fks = []
                indexes = []
            else:
                key = (schema_name, name)
                if all_cols is not None:
                    cols = all_cols.get(key, [])
                else:
                    cols = await self.fetch_columns(schema_name, name)

                if kind == "table":
                    if all_fks is not None:
                        fks = all_fks.get(key, [])
                    else:
                        fks = await self.fetch_foreign_keys(schema_name, name)

                    if all_indexes is not None:
                        indexes = all_indexes.get(key, [])
                    else:
                        indexes = await self.fetch_indexes(schema_name, name)
                else:
                    fks = []
                    indexes = []

            if kind == "table":
                obj = Table(
                    name=name,
                    schema_name=schema_name,
                    columns=cols,
                    foreign_keys=fks,
                    indexes=indexes,
                )
            else:
                obj = self.build_view(schema_name, name, kind, cols)
            schemas.setdefault(schema_name, []).append(obj)
        return SchemaSnapshot(
            connection_id=self.connection_id(),
            database=self.database_name(),
            schemas=schemas,
        )
