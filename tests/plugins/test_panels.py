"""Plugin panels actually mount into the TUI."""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from typing import TYPE_CHECKING  # noqa: E402

from textual.widgets import Static  # noqa: E402

from registro_core.config import RegistroConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_core.protocols.plugin import PluginManifest  # noqa: E402
from registro_plugins.api import Panel  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402

if TYPE_CHECKING:
    from registro_plugins.context import PluginContext
    from registro_plugins.loader import PluginLoader

_TEXT = "★ plugin-rail-ok"


class _RailPlugin:
    manifest = PluginManifest(id="acme.rail", name="Rail")

    def on_load(self, ctx: PluginContext) -> None:
        def _factory(_c: PluginContext) -> Static:
            return Static(_TEXT, id="acme-rail-text")

        ctx.register_panel(
            Panel(id="acme.rail.right", title="Acme", placement="right", factory=_factory)
        )

    def on_unload(self, ctx: PluginContext) -> None:
        # No cleanup needed for testing dummy plugin.
        pass


@pytest.mark.asyncio
async def test_no_rails_when_no_plugins() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        # Rails are mounted only when at least one panel exists.
        from textual.css.query import NoMatches  # noqa: PLC0415

        for selector in ("#right-rail", "#bottom-rail"):
            with pytest.raises(NoMatches):
                app.query_one(selector)
        grid = app.query_one("#main-grid")
        assert not grid.has_class("with-right")
        assert not grid.has_class("with-bottom")


@pytest.mark.asyncio
async def test_right_rail_panel_mounts() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        # Manually inject our plugin via the loader stored on the app.
        # The app loads entry-point plugins in ``on_mount``; we inject *after*
        # mount and then re-mount the panel ourselves to verify the seam.
        await pilot.pause()
        await app.workers.wait_for_complete()

        loader: PluginLoader = app._loader
        loader.load_instance(_RailPlugin())
        await app._mount_plugin_panels()
        await pilot.pause()
        # Look the static widget up by id.
        widget = app.query_one("#acme-rail-text", Static)
        assert _TEXT in str(widget.render())
        # The grid grew its layout to include the right rail.
        grid = app.query_one("#main-grid")
        assert grid.has_class("with-right")

        # The rail must be a *child of the grid*, not merely present somewhere.
        # It used to be mounted with ``before=self._status()``, and
        # ``Widget._find_mount_point`` resolves a widget spot to ``spot.parent``
        # — StatusBar's parent is MainScreen, not the grid. So the rail landed
        # outside the grid: the ``.with-right`` track it added was never
        # occupied and the rail rendered as a stacked block. Asserting only
        # "the widget exists" is what let that ship.
        rail = app.query_one("#right-rail")
        assert rail.parent is grid, f"right rail mounted under {rail.parent!r}, not #main-grid"
        assert app.query_one("#acme-rail-text").parent is rail


@pytest.mark.asyncio
async def test_bottom_rail_mounts_into_the_grid_after_the_right_rail() -> None:
    """Both rails at once: parents *and* child order, which drives grid placement."""

    class _BothRailsPlugin:
        manifest = PluginManifest(id="acme.both", name="Both")

        def on_load(self, ctx: PluginContext) -> None:
            ctx.register_panel(
                Panel(
                    id="acme.both.right",
                    title="R",
                    placement="right",
                    factory=lambda _c: Static("r", id="acme-r"),
                )
            )
            ctx.register_panel(
                Panel(
                    id="acme.both.bottom",
                    title="B",
                    placement="bottom",
                    factory=lambda _c: Static("b", id="acme-b"),
                )
            )

        def on_unload(self, ctx: PluginContext) -> None:
            pass

    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()

        loader: PluginLoader = app._loader
        loader.load_instance(_BothRailsPlugin())
        await app._mount_plugin_panels()
        await pilot.pause()

        grid = app.query_one("#main-grid")
        assert grid.has_class("with-right")
        assert grid.has_class("with-bottom")

        right = app.query_one("#right-rail")
        bottom = app.query_one("#bottom-rail")
        assert right.parent is grid
        assert bottom.parent is grid

        # The grid fills cells in child order, so sidebar/workspace come first,
        # then the right rail (third column), then the bottom rail (second row).
        ids = [child.id for child in grid.children]
        assert ids == ["sidebar", "workspace", "right-rail", "bottom-rail"], ids
