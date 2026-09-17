"""Schema introspection models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from registro_core.models.result import ColumnType

ObjectKind = Literal["table", "view", "materialized_view"]


class TableColumn(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    type: ColumnType = ColumnType.UNKNOWN
    type_native: str
    nullable: bool = True
    default: str | None = None
    is_primary_key: bool = False
    ordinal: int = 0


class Index(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    columns: list[str]
    unique: bool = False
    primary: bool = False


class ForeignKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    columns: list[str]
    referenced_schema: str | None = None
    referenced_table: str
    referenced_columns: list[str]
    on_delete: str | None = None
    on_update: str | None = None


class SchemaObject(BaseModel):
    """Common base for tables/views."""

    model_config = ConfigDict(frozen=True)

    name: str
    schema_name: str | None = None
    kind: ObjectKind
    columns: list[TableColumn] = Field(default_factory=list)
    indexes: list[Index] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    comment: str | None = None


class Table(SchemaObject):
    kind: ObjectKind = "table"


class View(SchemaObject):
    kind: ObjectKind = "view"
    definition: str | None = None


class SchemaSnapshot(BaseModel):
    """A point-in-time view of a database's schema graph."""

    model_config = ConfigDict(frozen=True)

    connection_id: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    database: str | None = None
    schemas: dict[str, list[SchemaObject]] = Field(default_factory=dict)

    @property
    def object_count(self) -> int:
        return sum(len(v) for v in self.schemas.values())
