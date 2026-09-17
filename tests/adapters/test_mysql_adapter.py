"""MySQL runs the shared adapter conformance suite.

Needs a server: either ``REGISTRO_MYSQL_DSN`` (see
``tests/scripts/test-integration.sh``) or a reachable Docker daemon, from which the
``mysql_dsn`` fixture starts a container.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("asyncmy")

from registro_adapters.mysql_adapter import MySQLAdapter
from registro_core.models.connection import Dsn
from registro_core.models.result import ColumnType
from tests.adapters._conformance import AdapterConformance, collect, drain

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration


class TestMySQLConformance(AdapterConformance):
    create_table_sql = "CREATE TABLE registro_conformance (id INT, name VARCHAR(32))"

    @pytest.fixture
    async def adapter(self, mysql_dsn: str) -> AsyncIterator[MySQLAdapter]:
        a = MySQLAdapter()
        await a.connect(Dsn.parse(mysql_dsn))
        try:
            await drain(a, "DROP TABLE IF EXISTS registro_conformance")
            yield a
        finally:
            await drain(a, "DROP TABLE IF EXISTS registro_conformance")
            await a.close()


@pytest.mark.asyncio
async def test_composite_foreign_key_introspection(mysql_dsn: str) -> None:
    dsn = Dsn.parse(mysql_dsn)
    adapter = MySQLAdapter()
    await adapter.connect(dsn)
    try:
        await drain(adapter, "DROP TABLE IF EXISTS fk_child")
        await drain(adapter, "DROP TABLE IF EXISTS fk_parent")
        await drain(adapter, "CREATE TABLE fk_parent (id1 INT, id2 INT, PRIMARY KEY (id1, id2))")
        await drain(
            adapter,
            "CREATE TABLE fk_child (c1 INT, c2 INT, "
            "FOREIGN KEY (c1, c2) REFERENCES fk_parent (id1, id2))",
        )

        fks = await adapter.fetch_foreign_keys(dsn.database or "test", "fk_child")
        assert len(fks) == 1
        assert fks[0].referenced_table == "fk_parent"
        assert fks[0].columns == ["c1", "c2"]
        assert fks[0].referenced_columns == ["id1", "id2"]
    finally:
        await drain(adapter, "DROP TABLE IF EXISTS fk_child")
        await drain(adapter, "DROP TABLE IF EXISTS fk_parent")
        await adapter.close()


@pytest.mark.asyncio
async def test_tinyint_and_boolean_columns_are_typed_as_integer(mysql_dsn: str) -> None:
    """End-to-end guard for the ``FIELD_TYPE.CHAR``/``TINY`` alias collision.

    MySQL spells ``BOOLEAN`` as ``TINYINT(1)`` and reports both over the wire as
    field type 1 — the same constant the driver also exposes as ``CHAR``. That
    code was mapped to STRING, so every boolean and tinyint column reached the
    results grid, CSV and JSON export as text. The ``CHAR(8)`` column is here to
    prove that fixing it did not untype genuine text.
    """
    adapter = MySQLAdapter()
    await adapter.connect(Dsn.parse(mysql_dsn))
    try:
        await drain(adapter, "DROP TABLE IF EXISTS tinyint_typing")
        await drain(
            adapter,
            "CREATE TABLE tinyint_typing (flag BOOLEAN, small TINYINT, txt CHAR(8))",
        )
        await drain(adapter, "INSERT INTO tinyint_typing VALUES (TRUE, 7, 'hi')")

        batches = await collect(adapter, "SELECT flag, small, txt FROM tinyint_typing")
        columns = next(b.columns for b in batches if b.columns)

        assert [c.type for c in columns] == [
            ColumnType.INTEGER,
            ColumnType.INTEGER,
            ColumnType.STRING,
        ]
    finally:
        await drain(adapter, "DROP TABLE IF EXISTS tinyint_typing")
        await adapter.close()
