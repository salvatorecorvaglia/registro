"""Domain models."""

from registro_core.models.connection import ConnectionInfo, Dsn
from registro_core.models.query import QueryHandle, QueryMetadata, QueryStatus
from registro_core.models.result import Column, ColumnType, ResultBatch, Row
from registro_core.models.schema import (
    ForeignKey,
    Index,
    SchemaObject,
    SchemaSnapshot,
    Table,
    TableColumn,
    View,
)

__all__ = [
    "Column",
    "ColumnType",
    "ConnectionInfo",
    "Dsn",
    "ForeignKey",
    "Index",
    "QueryHandle",
    "QueryMetadata",
    "QueryStatus",
    "ResultBatch",
    "Row",
    "SchemaObject",
    "SchemaSnapshot",
    "Table",
    "TableColumn",
    "View",
]
