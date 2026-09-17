"""Sample plugin used to test entry-point discovery.

This module is imported via a synthetic :class:`importlib.metadata.EntryPoint`
in ``tests/plugins/test_loader_discovery.py`` — it is not installed as a real
entry point. The shape mirrors what a third-party plugin would publish.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from registro_core.protocols.plugin import PluginManifest
from registro_plugins.api import Command

if TYPE_CHECKING:
    from registro_plugins.context import PluginContext


class SamplePlugin:
    manifest = PluginManifest(
        id="tests.sample",
        name="Sample (test fixture)",
        version="0.1.0",
        description="Used only by tests/plugins/test_loader_discovery.py.",
    )

    def on_load(self, ctx: PluginContext) -> None:
        async def _say_hi(_ctx: PluginContext) -> None:
            return

        ctx.register_command(Command(id="tests.sample.hi", title="Sample hi", handler=_say_hi))

    def on_unload(self, ctx: PluginContext) -> None:
        return


def factory() -> SamplePlugin:
    """Entry-point callable. Returns a fresh plugin instance per load."""
    return SamplePlugin()
