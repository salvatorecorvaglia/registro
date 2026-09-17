"""Database content must never be interpreted as Rich markup.

Result cells, schema identifiers and history previews all flow into widgets
that render markup. Values containing square brackets are entirely ordinary
(JSON arrays, regexes, Python reprs), and an unbalanced ``[`` raises
``rich.errors.MarkupError`` mid-render.
"""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.content import Content  # noqa: E402

from registro_core.models.result import Column, ResultBatch  # noqa: E402
from registro_core.models.schema import (  # noqa: E402
    ForeignKey,
    Index,
    SchemaSnapshot,
    Table,
    TableColumn,
)
from registro_tui.widgets.results_table import ResultsTable  # noqa: E402
from registro_tui.widgets.schema_tree import SchemaTree  # noqa: E402

_HOSTILE = "[bold]danger[/]"
# A bare closing tag is the crash case: Rich raises MarkupError on it.
_UNBALANCED = "[/]"


class _ResultsHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield ResultsTable()


class _TreeHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield SchemaTree()


@pytest.mark.asyncio
async def test_result_cells_render_markup_literally() -> None:
    app = _ResultsHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one(ResultsTable)
        await table.feed(
            ResultBatch(
                columns=[Column(name="payload"), Column(name=_HOSTILE)],
                rows=[(_HOSTILE, _UNBALANCED)],
            )
        )
        # The stored cell keeps the markup escaped...
        rendered = table.get_cell_at((0, 0))
        assert Text.from_markup(str(rendered)).plain == _HOSTILE
        # ...and the unbalanced bracket does not blow up rendering.
        assert Text.from_markup(str(table.get_cell_at((0, 1)))).plain == _UNBALANCED


@pytest.mark.asyncio
async def test_copy_cell_returns_the_raw_value_not_the_escaped_one() -> None:
    app = _ResultsHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one(ResultsTable)
        await table.feed(ResultBatch(columns=[Column(name="payload")], rows=[(_HOSTILE,)]))
        table.cursor_coordinate = (0, 0)  # type: ignore[assignment]
        assert table.current_cell_text() == _HOSTILE


@pytest.mark.asyncio
async def test_export_csv_writes_raw_values(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = _ResultsHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one(ResultsTable)
        await table.feed(ResultBatch(columns=[Column(name="payload")], rows=[(_HOSTILE,)]))
        target = tmp_path / "out.csv"
        assert await table.export_csv(target) == 1
        assert _HOSTILE in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_schema_identifiers_render_markup_literally() -> None:
    app = _TreeHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one(SchemaTree)
        table = Table(
            name=_HOSTILE,
            schema_name="main",
            columns=[TableColumn(name=_UNBALANCED, type_native="TEXT[]")],
            indexes=[Index(name=_HOSTILE, columns=["a"])],
            foreign_keys=[
                ForeignKey(
                    name="fk",
                    columns=[_HOSTILE],
                    referenced_table=_UNBALANCED,
                    referenced_columns=["id"],
                )
            ],
        )
        tree.populate(
            SchemaSnapshot(connection_id="c", database=_HOSTILE, schemas={"main": [table]})
        )

        labels = [str(tree.root.label)]
        stack = list(tree.root.children)
        while stack:
            node = stack.pop()
            labels.append(str(node.label))
            stack.extend(node.children)
        assert _HOSTILE in labels

        # Filling an object node must not raise on the unbalanced bracket.
        object_node = tree.root.children[0].children[0].children[0]
        tree._fill_object_node(object_node, table)
        column_labels = [
            str(leaf.label) for group in object_node.children for leaf in group.children
        ]
        assert any(_UNBALANCED in label for label in column_labels)


# ---- error-reporting paths ----
#
# The widget layer escapes thoroughly, but the *error* paths did not: they are
# the one surface that renders driver text, and driver text is full of brackets.


async def test_connection_modal_survives_a_bracket_bearing_dsn() -> None:
    """Typing ``[/]`` into the DSN field must show an error, not raise.

    ``Dsn.parse`` embeds the offending input in its message and the modal feeds
    that to ``Static.update()``, which parses markup — so an unbalanced tag
    raised ``MarkupError`` from inside the modal's own render.
    """
    from textual.widgets import Input, Static  # noqa: PLC0415

    from registro_tui.screens.connection import ConnectionScreen  # noqa: PLC0415

    class _Harness(App[None]):
        pass

    app = _Harness()
    async with app.run_test() as pilot:
        await app.push_screen(ConnectionScreen())
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConnectionScreen)
        screen.query_one("#conn-input", Input).value = _UNBALANCED
        screen.submit()
        await pilot.pause()

        # Still open (submit was rejected) and the error is rendered as text.
        assert isinstance(app.screen, ConnectionScreen)
        # ``.content`` holds the markup source. Parsing it is what the render
        # does, so this both proves it will not raise and shows what the user
        # actually reads: the escaped brackets, restored.
        content = screen.query_one("#conn-error", Static).content
        assert _UNBALANCED in Text.from_markup(content).plain


async def test_error_toast_preserves_bracketed_driver_text() -> None:
    """``_report_error`` must not let a driver message be parsed as markup.

    ``syntax error near [col]`` rendered as ``syntax error near `` — Rich ate
    the one identifier that mattered — and ``[/]`` raised MarkupError inside the
    toast's render.
    """
    from registro_tui.app import RegistroApp  # noqa: PLC0415

    detail = f"syntax error near [col] {_UNBALANCED}"
    app = RegistroApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._report_error("Query failed", detail)
        await pilot.pause()

        notification = next(iter(app._notifications))
        assert notification.message == detail
        assert notification.markup is False


# ---- theme variables vs. the two markup parsers ----
#
# Textual's markup understands ``[$error]``; rich's does not — it treats the
# tag as literal text and then raises MarkupError on the unmatched ``[/]``. Which
# parser a widget uses is therefore load-bearing, and it is not obvious from the
# calling code: Static renders Textual Content, but Tree labels are rich Text.


def test_rich_and_textual_markup_disagree_about_theme_variables() -> None:
    """Documents the trap these tests exist to catch."""
    from rich.errors import MarkupError as RichMarkupError  # noqa: PLC0415
    from textual.content import Content  # noqa: PLC0415

    assert Content.from_markup("[$error]boom[/]").plain == "boom"
    with pytest.raises(RichMarkupError):
        Text.from_markup("[$error]boom[/]")


async def test_status_bar_renders_themed_severity_messages() -> None:
    """The footer measures its own width, so it must parse what it renders.

    It measured with rich's ``Text`` while rendering through Textual, so the
    first ``[$error]`` message would have raised MarkupError mid-render.
    """
    from registro_tui.widgets.status_bar import StatusBar  # noqa: PLC0415

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield StatusBar()

    app = _Harness()
    async with app.run_test() as pilot:
        bar = app.query_one(StatusBar)
        bar.message = "[$error]Query failed: boom[/]"
        await pilot.pause()
        rendered = bar.render()
        assert "Query failed: boom" in Content.from_markup(str(rendered)).plain


async def test_schema_tree_labels_stay_parseable_by_rich() -> None:
    """Tree labels go through rich, so they must not carry ``$variable`` tags."""
    from registro_core.models.schema import SchemaSnapshot, Table, TableColumn  # noqa: PLC0415

    snapshot = SchemaSnapshot(
        connection_id="c",
        database="db",
        schemas={
            "main": [
                Table(
                    name="t",
                    schema_name="main",
                    columns=[
                        TableColumn(name="id", type_native="INTEGER", is_primary_key=True),
                        TableColumn(name="req", type_native="TEXT", nullable=False),
                    ],
                )
            ]
        },
    )

    app = _TreeHarness()
    async with app.run_test() as pilot:
        tree = app.query_one(SchemaTree)
        tree.populate(snapshot)
        await pilot.pause()
        node = next(n for n in tree.root.children[0].children[0].children if n.data is not None)
        node.expand()
        await pilot.pause()

        labels = [str(child.label) for child in node.children[0].children]
        assert any("(PK)" in lbl for lbl in labels)
        assert any("NOT NULL" in lbl for lbl in labels)
        for label in labels:
            # The real assertion: rich can parse every one of them.
            Text.from_markup(label)
