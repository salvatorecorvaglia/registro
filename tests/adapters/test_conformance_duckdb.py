"""DuckDB runs the shared adapter conformance suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("duckdb")

from registro_adapters.duckdb_adapter import DuckDBAdapter
from registro_core.models.connection import Dsn
from tests.adapters._conformance import AdapterConformance

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class TestDuckDBConformance(AdapterConformance):
    create_table_sql = "CREATE TABLE registro_conformance (id INTEGER, name VARCHAR)"

    @pytest.fixture
    async def adapter(self) -> AsyncIterator[DuckDBAdapter]:
        a = DuckDBAdapter()
        await a.connect(Dsn.parse("duckdb:///:memory:"))
        try:
            yield a
        finally:
            await a.close()
