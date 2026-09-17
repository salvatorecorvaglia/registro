from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from registro_core.events import ConnectionOpened, EventBus
from registro_core.protocols.plugin import PluginManifest
from registro_plugins.api import Command, hook, hook_event, is_hook
from registro_plugins.loader import PluginLoader
from registro_plugins.registry import PluginRegistry

if TYPE_CHECKING:
    from registro_plugins.context import PluginContext


class _SamplePlugin:
    manifest = PluginManifest(id="sample", name="Sample", version="1.0.0")

    def __init__(self) -> None:
        self.loaded = False

    def on_load(self, ctx: PluginContext) -> None:
        async def _noop(ctx: PluginContext) -> None:
            return

        ctx.register_command(Command(id="sample.hello", title="Hello", handler=_noop))
        self.loaded = True

    def on_unload(self, ctx: PluginContext) -> None:
        self.loaded = False


@pytest.mark.asyncio
async def test_loader_registers_plugin_and_command() -> None:
    bus = EventBus()
    registry = PluginRegistry()
    loader = PluginLoader(registry, bus)
    plugin = _SamplePlugin()
    loader.load_instance(plugin)
    assert plugin.loaded
    cmds = registry.all_commands()
    assert any(c.id == "sample.hello" for c in cmds)
    loader.unload("sample")
    assert not plugin.loaded
    # let publish drain
    for _ in range(5):  # type: ignore[unreachable]
        await asyncio.sleep(0)
    await bus.close()


def test_hook_decorator_marks_function() -> None:
    @hook("query.before_execute")
    async def my_hook(_ctx: object) -> None:
        return

    assert is_hook(my_hook)
    assert hook_event(my_hook) == "query.before_execute"


class _ServicesProbe:
    """Plugin that snapshots the services dict during on_load."""

    manifest = PluginManifest(id="probe", name="Probe", version="1.0.0")

    def __init__(self) -> None:
        self.eager: object = None
        self.late_after_update: object = None
        self.ctx: PluginContext | None = None

    def on_load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.eager = ctx.get_service("event_bus")

    def on_unload(self, ctx: PluginContext) -> None:
        return


def _ids(registry: PluginRegistry) -> set[str]:
    return {m.id for m in registry.manifests()}


@pytest.mark.asyncio
async def test_disabled_plugin_is_not_loaded() -> None:
    registry = PluginRegistry()
    loader = PluginLoader(registry, EventBus(), disabled=["sample"])
    loader.load_instance(_SamplePlugin())
    assert _ids(registry) == set()
    assert registry.all_commands() == []


@pytest.mark.asyncio
async def test_enabled_allowlist_excludes_unlisted_plugins() -> None:
    registry = PluginRegistry()
    loader = PluginLoader(registry, EventBus(), enabled=["other"])
    loader.load_instance(_SamplePlugin())
    assert _ids(registry) == set()


@pytest.mark.asyncio
async def test_enabled_allowlist_loads_listed_plugin() -> None:
    registry = PluginRegistry()
    loader = PluginLoader(registry, EventBus(), enabled=["sample"])
    loader.load_instance(_SamplePlugin())
    assert _ids(registry) == {"sample"}


@pytest.mark.asyncio
async def test_incompatible_plugin_version_is_skipped() -> None:
    registry = PluginRegistry()
    loader = PluginLoader(registry, EventBus())

    class _FuturePlugin:
        manifest = PluginManifest(
            id="future", name="Future", version="1.0.0", requires_registro=">=99.0.0"
        )

        def on_load(self, ctx: PluginContext) -> None:
            return

        def on_unload(self, ctx: PluginContext) -> None:
            return

    loader.load_instance(_FuturePlugin())
    assert _ids(registry) == set()


@pytest.mark.asyncio
async def test_services_are_exposed_at_load_and_updatable() -> None:
    bus = EventBus()
    registry = PluginRegistry()
    loader = PluginLoader(registry, bus, services={"event_bus": bus, "thing": "v1"})
    probe = _ServicesProbe()
    loader.load_instance(probe)

    # Eager services were visible inside on_load.
    assert probe.eager is bus
    assert probe.ctx is not None
    assert probe.ctx.get_service("thing") == "v1"

    # Late-bound services attach via update_services and stay readable.
    sentinel = object()
    probe.ctx.update_services(executor=sentinel)
    assert probe.ctx.get_service("executor") is sentinel

    await bus.close()


@pytest.mark.asyncio
async def test_loader_recovery_from_collision_or_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Create a successful plugin
    class SuccessPlugin:
        manifest = PluginManifest(id="success_plugin", name="Success", version="1.0.0")

        def on_load(self, ctx: PluginContext) -> None:
            pass

    class MockEntryPoint:
        def __init__(self, name: str, load_fn: Any) -> None:
            self.name = name
            self.load = load_fn

    # 2. Create a mock entry point that fails
    def mock_load_fail() -> type:
        raise RuntimeError("simulated instantiation failure")

    ep_fail = MockEntryPoint(name="fail_plugin", load_fn=mock_load_fail)

    # 3. Create a mock entry point that succeeds
    ep_success = MockEntryPoint(name="success_plugin", load_fn=lambda: SuccessPlugin)

    registry = PluginRegistry()
    bus = EventBus()
    loader = PluginLoader(registry, bus)

    # Mock loader.discover to return both entry points
    monkeypatch.setattr(loader, "discover", lambda: [ep_fail, ep_success])

    # Call load_all() and verify it doesn't raise, and succeeds in loading the second plugin!
    loader.load_all()

    # The success plugin should be registered
    assert "success_plugin" in {m.id for m in registry.manifests()}
    # The fail plugin should not be registered
    assert "fail_plugin" not in {m.id for m in registry.manifests()}

    await bus.close()


@pytest.mark.asyncio
async def test_loader_unsubscribes_on_unload() -> None:
    bus = EventBus()
    registry = PluginRegistry()
    loader = PluginLoader(registry, bus)

    called = 0

    class SubscriptionPlugin:
        manifest = PluginManifest(id="sub_plugin", name="Sub", version="1.0.0")

        def on_load(self, ctx: PluginContext) -> None:
            async def _handler(ev: ConnectionOpened) -> None:
                nonlocal called
                called += 1

            ctx.subscribe(ConnectionOpened, _handler)

        def on_unload(self, ctx: PluginContext) -> None:
            pass

    plugin = SubscriptionPlugin()
    loader.load_instance(plugin)

    bus.publish(ConnectionOpened(connection_id="1", dsn_scheme="sqlite"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert called == 1

    loader.unload("sub_plugin")

    bus.publish(ConnectionOpened(connection_id="2", dsn_scheme="sqlite"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert called == 1

    await bus.close()
