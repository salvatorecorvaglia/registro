from __future__ import annotations

import asyncio

import pytest

from registro_core.events import Event, EventBus, QueryStarted
from registro_core.protocols.plugin import PluginManifest
from registro_plugins.api import Command
from registro_plugins.context import PluginContext
from registro_plugins.registry import PluginRegistry


def _make_context(services: dict[str, object] | None = None) -> PluginContext:
    return PluginContext(
        plugin_id="sample",
        event_bus=EventBus(),
        registry=PluginRegistry(),
        services=services,
    )


def test_get_service_returns_none_for_unknown_service() -> None:
    ctx = _make_context()
    assert ctx.get_service("nope") is None


def test_get_service_returns_eagerly_provided_services() -> None:
    ctx = _make_context({"config": "cfg-object"})
    assert ctx.get_service("config") == "cfg-object"


def test_update_services_makes_new_entries_readable() -> None:
    ctx = _make_context()
    sentinel = object()
    ctx.update_services(executor=sentinel)
    assert ctx.get_service("executor") is sentinel


def test_shared_services_mapping_is_copied_not_aliased() -> None:
    """The loader hands the same ``services`` dict to every plugin context;
    one plugin calling ``update_services`` must not rewrite what another
    plugin resolves via ``get_service``."""
    shared: dict[str, object] = {"event_bus": "bus"}
    ctx_a = PluginContext(
        plugin_id="a", event_bus=EventBus(), registry=PluginRegistry(), services=shared
    )
    ctx_b = PluginContext(
        plugin_id="b", event_bus=EventBus(), registry=PluginRegistry(), services=shared
    )

    ctx_a.update_services(only_for_a="value")

    assert ctx_a.get_service("only_for_a") == "value"
    assert ctx_b.get_service("only_for_a") is None
    assert "only_for_a" not in shared


@pytest.mark.asyncio
async def test_subscribe_delivers_events_through_the_bus() -> None:
    bus = EventBus()
    ctx = PluginContext(plugin_id="sample", event_bus=bus, registry=PluginRegistry())
    delivered = asyncio.Event()
    seen: list[QueryStarted] = []

    async def handler(ev: QueryStarted) -> None:
        seen.append(ev)
        delivered.set()

    ctx.subscribe(QueryStarted, handler)
    ctx.publish(QueryStarted(query_id="q", connection_id="c", sql_preview="select 1"))
    await asyncio.wait_for(delivered.wait(), timeout=1)

    assert len(seen) == 1
    await bus.close()


@pytest.mark.asyncio
async def test_close_unsubscribes_every_handler_registered_through_the_context() -> None:
    bus = EventBus()
    ctx = PluginContext(plugin_id="sample", event_bus=bus, registry=PluginRegistry())
    seen: list[Event] = []

    async def handler(ev: QueryStarted) -> None:
        seen.append(ev)

    ctx.subscribe(QueryStarted, handler)
    ctx.close()

    ctx.publish(QueryStarted(query_id="q", connection_id="c", sql_preview="select 1"))
    for _ in range(20):
        await asyncio.sleep(0)

    assert seen == []
    await bus.close()


def test_register_command_delegates_to_the_registry_under_this_plugin_id() -> None:
    registry = PluginRegistry()
    ctx = PluginContext(plugin_id="sample", event_bus=EventBus(), registry=registry)

    async def _handler(_ctx: PluginContext) -> None:
        return None

    cmd = Command(id="sample.cmd", title="Sample", handler=_handler)
    ctx.register_command(cmd)

    assert registry.all_commands() == [cmd]
    assert registry.get_plugin_for_command("sample.cmd") == "sample"


def test_register_command_collision_raises_via_the_registry() -> None:
    registry = PluginRegistry()
    registry.add_plugin(PluginManifest(id="other", name="Other"))
    ctx = PluginContext(plugin_id="sample", event_bus=EventBus(), registry=registry)

    async def _handler(_ctx: PluginContext) -> None:
        return None

    registry.add_command("other", Command(id="dup", title="Dup", handler=_handler))

    with pytest.raises(Exception, match="command id collision"):
        ctx.register_command(Command(id="dup", title="Dup 2", handler=_handler))
