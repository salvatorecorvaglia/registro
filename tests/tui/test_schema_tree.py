"""Schema tree lazy-population tests."""

from __future__ import annotations

import pytest

from registro_core.models.result import ColumnType
from registro_core.models.schema import SchemaSnapshot, Table, TableColumn

textual = pytest.importorskip("textual")

from registro_core.config import RegistroConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402
from registro_tui.widgets import SchemaTree  # noqa: E402


def _snapshot() -> SchemaSnapshot:
    table = Table(
        name="users",
        schema_name="main",
        columns=[
            TableColumn(
                name="id",
                type_native="INTEGER",
                type=ColumnType.INTEGER,
                is_primary_key=True,
                ordinal=0,
            ),
            TableColumn(name="email", type_native="TEXT", type=ColumnType.STRING, ordinal=1),
        ],
    )
    return SchemaSnapshot(connection_id="x", database="demo", schemas={"main": [table]})


def _walk_labels(tree: SchemaTree) -> list[str]:
    """Collect rendered labels of every node currently in the tree."""
    labels: list[str] = []
    stack = [tree.root]
    while stack:
        node = stack.pop()
        labels.append(str(node.label))
        stack.extend(node.children)
    return labels


@pytest.mark.asyncio
async def test_columns_only_appear_after_expansion() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        tree = app.query_one(SchemaTree)
        tree.populate(_snapshot())
        await pilot.pause()

        before = _walk_labels(tree)
        # The "users" object node exists, but neither "columns" header nor
        # the "id" column should be rendered yet.
        assert any("users" in lbl for lbl in before)
        assert not any(lbl == "columns" for lbl in before)
        assert not any("id" in lbl and "INTEGER" in lbl for lbl in before)

        # Find the users node and expand it.
        users_node = next(
            n
            for n in (
                tree.root.children[0].children[0].children  # schema → Tables → users
            )
            if str(n.label) == "users"
        )
        users_node.expand()
        await pilot.pause()

        after = _walk_labels(tree)
        assert any(lbl == "columns" for lbl in after)
        assert any("id" in lbl and "INTEGER" in lbl for lbl in after)


@pytest.mark.asyncio
async def test_re_expansion_does_not_duplicate() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        tree = app.query_one(SchemaTree)
        tree.populate(_snapshot())
        await pilot.pause()
        users_node = next(
            n for n in tree.root.children[0].children[0].children if str(n.label) == "users"
        )
        users_node.expand()
        await pilot.pause()
        users_node.collapse()
        users_node.expand()
        await pilot.pause()
        # Expect exactly one "columns" subnode after re-expansion.
        column_headers = [c for c in users_node.children if str(c.label) == "columns"]
        assert len(column_headers) == 1


@pytest.mark.asyncio
async def test_introspection_retry_on_failure() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        tree = app.query_one(SchemaTree)

        # Prepare a table object that does not have columns initially loaded
        table_lazy = Table(name="lazy_table", schema_name="main", columns=[])
        snap = SchemaSnapshot(connection_id="x", database="demo", schemas={"main": [table_lazy]})
        tree.populate(snap)
        await pilot.pause()

        # Mock introspector to raise exception first time
        introspector = app.state.introspector
        assert introspector is not None

        call_count = 0

        async def mock_fetch(schema: str, name: str) -> list[TableColumn]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Temporary DB Error")
            return [
                TableColumn(name="id", type_native="INTEGER", type=ColumnType.INTEGER, ordinal=0)
            ]

        introspector.fetch_columns = mock_fetch  # type: ignore[method-assign]

        # Find the lazy_table node
        lazy_node = next(
            n for n in tree.root.children[0].children[0].children if str(n.label) == "lazy_table"
        )

        # 1. Expand first time - fails and catches error
        lazy_node.expand()
        await pilot.pause()

        assert len(lazy_node.children) == 0
        # The widget posts IntrospectionFailed rather than writing to the status
        # bar itself; the app's handler is what surfaces it.
        assert "lazy_table" in app._status().message

        # 2. Collapse and expand again - succeeds
        lazy_node.collapse()
        lazy_node.expand()
        await pilot.pause()

        assert len(lazy_node.children) > 0
        assert any(str(c.label) == "columns" for c in lazy_node.children)
