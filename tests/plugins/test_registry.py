"""Tests for PluginRegistry collision guards."""

from __future__ import annotations

import pytest

from registro_core.errors import PluginError
from registro_plugins.api import Command, Panel
from registro_plugins.registry import PluginRegistry


async def _noop(_ctx: object) -> None:
    return


def _command(cmd_id: str) -> Command:
    return Command(id=cmd_id, title="X", handler=_noop)


def _panel(panel_id: str) -> Panel:
    return Panel(id=panel_id, title="P", placement="left", factory=lambda _ctx: None)


def test_add_command_rejects_duplicate_id_across_plugins() -> None:
    reg = PluginRegistry()
    reg.add_command("plugin-a", _command("shared"))
    with pytest.raises(PluginError, match="command id collision"):
        reg.add_command("plugin-b", _command("shared"))


def test_add_panel_rejects_duplicate_id_across_plugins() -> None:
    reg = PluginRegistry()
    reg.add_panel("plugin-a", _panel("shared"))
    with pytest.raises(PluginError, match="panel id collision"):
        reg.add_panel("plugin-b", _panel("shared"))


def test_distinct_ids_register_cleanly() -> None:
    reg = PluginRegistry()
    reg.add_command("plugin-a", _command("one"))
    reg.add_command("plugin-b", _command("two"))
    reg.add_panel("plugin-a", _panel("p-one"))
    assert {c.id for c in reg.all_commands()} == {"one", "two"}
    assert {p.id for p in reg.all_panels()} == {"p-one"}


def test_get_plugin_for_command() -> None:
    reg = PluginRegistry()
    reg.add_command("plugin-a", _command("one"))
    reg.add_command("plugin-b", _command("two"))
    assert reg.get_plugin_for_command("one") == "plugin-a"
    assert reg.get_plugin_for_command("two") == "plugin-b"
    assert reg.get_plugin_for_command("nonexistent") is None
