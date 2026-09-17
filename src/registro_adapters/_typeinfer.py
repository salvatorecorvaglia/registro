"""Map driver-native type names to Registro ``ColumnType``."""

from __future__ import annotations

import datetime
import decimal
import uuid

from registro_core.models.result import ColumnType

_INTEGER = {
    "int",
    "int2",
    "int4",
    "int8",
    "integer",
    "smallint",
    "bigint",
    "tinyint",
    "mediumint",
    "serial",
    "bigserial",
}
_FLOAT = {"real", "float", "float4", "float8", "double", "double precision"}
_DECIMAL = {"decimal", "numeric"}
_BOOL = {"bool", "boolean", "bit"}
_DATE = {"date"}
_TIME = {"time", "timetz"}
_DATETIME = {"timestamp", "timestamptz", "datetime"}
_STRING = {
    "text",
    "varchar",
    "char",
    "character",
    "character varying",
    "string",
    "name",
    "citext",
    "enum",
    "set",
}
_JSON = {"json", "jsonb"}
_BINARY = {"bytea", "blob", "binary", "varbinary"}
_UUID = {"uuid"}


def infer_column_type_from_value(value: object) -> ColumnType:
    """Infer a column type from a sample value.

    SQLite's cursor description carries only column names — no types at all —
    so for that adapter the value itself is the only available evidence.
    ``None`` yields ``UNKNOWN`` rather than ``NULL``: a NULL in the first row
    says nothing about the column.
    """
    if value is None:
        return ColumnType.UNKNOWN
    if isinstance(value, bool):
        return ColumnType.BOOL
    if isinstance(value, int):
        return ColumnType.INTEGER
    if isinstance(value, float):
        return ColumnType.FLOAT
    if isinstance(value, decimal.Decimal):
        return ColumnType.DECIMAL
    if isinstance(value, datetime.datetime):
        return ColumnType.DATETIME
    if isinstance(value, datetime.date):
        return ColumnType.DATE
    if isinstance(value, datetime.time):
        return ColumnType.TIME
    if isinstance(value, uuid.UUID):
        return ColumnType.UUID
    if isinstance(value, bytes | bytearray | memoryview):
        return ColumnType.BINARY
    if isinstance(value, str):
        return ColumnType.STRING
    if isinstance(value, dict | list):
        return ColumnType.JSON
    return ColumnType.UNKNOWN


def infer_column_type(native: str | None) -> ColumnType:
    if not native:
        return ColumnType.UNKNOWN
    n = native.strip().lower()
    # Drop parametric suffix: VARCHAR(255) -> varchar
    if "(" in n:
        n = n.split("(", 1)[0].strip()
    if n in _INTEGER:
        return ColumnType.INTEGER
    if n in _FLOAT:
        return ColumnType.FLOAT
    if n in _DECIMAL:
        return ColumnType.DECIMAL
    if n in _BOOL:
        return ColumnType.BOOL
    if n in _DATE:
        return ColumnType.DATE
    if n in _TIME:
        return ColumnType.TIME
    if n in _DATETIME:
        return ColumnType.DATETIME
    if n in _STRING:
        return ColumnType.STRING
    if n in _JSON:
        return ColumnType.JSON
    if n in _BINARY:
        return ColumnType.BINARY
    if n in _UUID:
        return ColumnType.UUID
    return ColumnType.UNKNOWN
