"""Result row / batch models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

Row = tuple[Any, ...]


class ColumnType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOL = "bool"
    DATE = "date"
    TIME = "time"
    DATETIME = "datetime"
    JSON = "json"
    BINARY = "binary"
    UUID = "uuid"
    NULL = "null"
    UNKNOWN = "unknown"


class Column(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    type: ColumnType = ColumnType.UNKNOWN
    type_native: str | None = None
    nullable: bool = True


class ResultBatch(BaseModel):
    """A chunk of rows streamed from an adapter."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    columns: list[Column]
    rows: list[Row] = Field(default_factory=list)
    is_last: bool = False

    @property
    def width(self) -> int:
        return len(self.columns)

    @property
    def height(self) -> int:
        return len(self.rows)
