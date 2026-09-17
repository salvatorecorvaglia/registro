"""PostgreSQL runs the shared adapter conformance suite.

Needs a server: either ``REGISTRO_PG_DSN`` (see ``tests/scripts/test-integration.sh``)
or a reachable Docker daemon, from which the ``postgres_dsn`` fixture starts a
container. Marked ``integration`` so the default local run stays fast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("asyncpg")

from registro_adapters.postgres_adapter import PostgresAdapter
from registro_core.models.connection import Dsn
from tests.adapters._conformance import AdapterConformance, drain

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration


class TestPostgresConformance(AdapterConformance):
    create_table_sql = "CREATE TABLE registro_conformance (id INTEGER, name VARCHAR(32))"

    @pytest.fixture
    async def adapter(self, postgres_dsn: str) -> AsyncIterator[PostgresAdapter]:
        a = PostgresAdapter()
        await a.connect(Dsn.parse(postgres_dsn))
        try:
            # The server is shared across the session, so each test starts from
            # a clean table rather than inheriting the previous one's rows.
            await drain(a, "DROP TABLE IF EXISTS registro_conformance")
            yield a
        finally:
            await drain(a, "DROP TABLE IF EXISTS registro_conformance")
            await a.close()


@pytest.mark.asyncio
async def test_composite_foreign_key_introspection(postgres_dsn: str) -> None:
    adapter = PostgresAdapter()
    await adapter.connect(Dsn.parse(postgres_dsn))
    try:
        await drain(adapter, "DROP TABLE IF EXISTS fk_child")
        await drain(adapter, "DROP TABLE IF EXISTS fk_parent")
        await drain(adapter, "CREATE TABLE fk_parent (id1 INT, id2 INT, PRIMARY KEY (id1, id2))")
        await drain(
            adapter,
            "CREATE TABLE fk_child (c1 INT, c2 INT, "
            "FOREIGN KEY (c1, c2) REFERENCES fk_parent (id1, id2))",
        )

        fks = await adapter.fetch_foreign_keys("public", "fk_child")
        assert len(fks) == 1
        assert fks[0].referenced_table == "fk_parent"
        assert fks[0].columns == ["c1", "c2"]
        assert fks[0].referenced_columns == ["id1", "id2"]
    finally:
        await drain(adapter, "DROP TABLE IF EXISTS fk_child")
        await drain(adapter, "DROP TABLE IF EXISTS fk_parent")
        await adapter.close()
