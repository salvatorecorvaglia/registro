"""Runtime registry of loaded plugins, commands, and panels."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from registro_core.errors import PluginError
from registro_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from registro_core.protocols.plugin import PluginManifest
    from registro_plugins.api import Command, Panel

logger = get_logger(__name__)


class PluginRegistry:
    def __init__(self) -> None:
        self._manifests: dict[str, PluginManifest] = {}
        self._commands_by_plugin: dict[str, list[Command]] = defaultdict(list)
        self._panels_by_plugin: dict[str, list[Panel]] = defaultdict(list)

    def add_plugin(self, manifest: PluginManifest) -> None:
        if manifest.id in self._manifests:
            raise PluginError(
                f"plugin id collision: {manifest.id!r} already registered",
                context={"plugin_id": manifest.id},
            )
        self._manifests[manifest.id] = manifest

    def remove_plugin(self, plugin_id: str) -> None:
        self._manifests.pop(plugin_id, None)
        self._commands_by_plugin.pop(plugin_id, None)
        self._panels_by_plugin.pop(plugin_id, None)

    def add_command(self, plugin_id: str, command: Command) -> None:
        for owner, existing in self._commands_by_plugin.items():
            if any(c.id == command.id for c in existing):
                raise PluginError(
                    f"command id collision: {command.id!r} already registered by plugin {owner!r}",
                    context={"plugin_id": plugin_id, "command_id": command.id},
                )
        self._commands_by_plugin[plugin_id].append(command)
        logger.debug("plugin_command_registered", plugin_id=plugin_id, command_id=command.id)

    def add_panel(self, plugin_id: str, panel: Panel) -> None:
        for owner, existing in self._panels_by_plugin.items():
            if any(p.id == panel.id for p in existing):
                raise PluginError(
                    f"panel id collision: {panel.id!r} already registered by plugin {owner!r}",
                    context={"plugin_id": plugin_id, "panel_id": panel.id},
                )
        self._panels_by_plugin[plugin_id].append(panel)
        logger.debug("plugin_panel_registered", plugin_id=plugin_id, panel_id=panel.id)

    def manifests(self) -> Iterable[PluginManifest]:
        return self._manifests.values()

    def all_commands(self) -> list[Command]:
        return [cmd for cmds in self._commands_by_plugin.values() for cmd in cmds]

    def get_plugin_for_command(self, command_id: str) -> str | None:
        """Find the plugin ID that registered a command by its ID."""
        for plugin_id, commands in self._commands_by_plugin.items():
            if any(cmd.id == command_id for cmd in commands):
                return plugin_id
        return None

    def all_panels(self) -> list[Panel]:
        return [p for panels in self._panels_by_plugin.values() for p in panels]

    def panels_by_plugin(self) -> list[tuple[str, Panel]]:
        return [
            (plugin_id, panel)
            for plugin_id, panels in self._panels_by_plugin.items()
            for panel in panels
        ]
