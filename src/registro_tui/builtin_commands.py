"""Built-in commands surfaced in the palette.

These mirror keyboard shortcuts but make the action discoverable via Ctrl+P.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from registro_plugins.api import Command

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from registro_plugins.context import PluginContext


from registro_tui.events import RunActionRequested
from registro_tui.keymaps.default import ACTION_TO_KEY


def _wrap(action: str) -> Callable[[PluginContext], Awaitable[None]]:
    async def _handler(ctx: PluginContext) -> None:
        # Built-ins are invoked through the app's action_* methods. We use
        # the event bus to publish a request the app routes back to itself.
        ctx.publish(RunActionRequested(action=action))

    return _handler


BUILTIN_COMMANDS: list[Command] = [
    Command(
        id="builtin.run_query",
        title="Run query",
        description="Execute the SQL in the active tab.",
        default_binding=ACTION_TO_KEY.get("run_query"),
        handler=_wrap("run_query"),
        category="Query",
    ),
    Command(
        id="builtin.cancel_query",
        title="Cancel running query",
        default_binding=ACTION_TO_KEY.get("cancel_query"),
        handler=_wrap("cancel_query"),
        category="Query",
    ),
    Command(
        id="builtin.refresh_schema",
        title="Refresh schema",
        default_binding=ACTION_TO_KEY.get("refresh_schema"),
        handler=_wrap("refresh_schema"),
        category="Schema",
    ),
    Command(
        id="builtin.show_history",
        title="Show query history",
        default_binding=ACTION_TO_KEY.get("show_history"),
        handler=_wrap("show_history"),
        category="Query",
    ),
    Command(
        id="builtin.new_tab",
        title="New query tab",
        default_binding=ACTION_TO_KEY.get("new_tab"),
        handler=_wrap("new_tab"),
        category="Tabs",
    ),
    Command(
        id="builtin.close_tab",
        title="Close current tab",
        default_binding=ACTION_TO_KEY.get("close_tab"),
        handler=_wrap("close_tab"),
        category="Tabs",
    ),
    Command(
        id="builtin.copy_cell",
        title="Copy current cell",
        description="Copy the focused result cell to the system clipboard.",
        default_binding=ACTION_TO_KEY.get("copy_cell"),
        handler=_wrap("copy_cell"),
        category="Results",
    ),
    Command(
        id="builtin.export_csv",
        title="Export results as CSV",
        description="Write the current result set to ~/.registro/exports/.",
        default_binding=ACTION_TO_KEY.get("export_csv"),
        handler=_wrap("export_csv"),
        category="Results",
    ),
    Command(
        id="builtin.connect",
        title="Connect to database",
        description="Open the connection dialog to connect to a new database.",
        handler=_wrap("connect"),
        category="App",
    ),
    Command(
        id="builtin.set_row_limit",
        title="Set row display limit",
        description="Change the maximum row display limit dynamically.",
        handler=_wrap("set_row_limit"),
        category="Results",
    ),
    Command(
        id="builtin.help",
        title="Show help",
        description="List the currently active keybindings.",
        default_binding=ACTION_TO_KEY.get("help"),
        handler=_wrap("help"),
        category="App",
    ),
    Command(
        id="builtin.quit",
        title="Quit Registro",
        default_binding=ACTION_TO_KEY.get("quit"),
        handler=_wrap("quit"),
        category="App",
    ),
]
