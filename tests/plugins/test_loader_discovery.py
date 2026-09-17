"""Cover the real entry-point discovery path of ``PluginLoader``.

We don't install a real wheel — we monkeypatch
``registro_plugins.loader.entry_points`` to return a synthetic
``EntryPoint`` pointing at the in-tree fixture module.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from registro_core.errors import PluginLoadError
from registro_core.events import EventBus
from registro_plugins.loader import PluginLoader
from registro_plugins.registry import PluginRegistry


@pytest.mark.asyncio
async def test_discover_returns_entry_points_filtered_by_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = EntryPoint(
        name="sample",
        value="tests.fixtures.sample_plugin:factory",
        group=PluginLoader.GROUP,
    )

    def _fake_entry_points(*, group: str) -> tuple[EntryPoint, ...]:
        assert group == PluginLoader.GROUP
        return (fake,)

    monkeypatch.setattr("registro_plugins.loader.entry_points", _fake_entry_points)

    bus = EventBus()
    try:
        loader = PluginLoader(PluginRegistry(), bus)
        found = loader.discover()
        assert [ep.name for ep in found] == ["sample"]
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_load_all_drives_entry_point_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = EntryPoint(
        name="sample",
        value="tests.fixtures.sample_plugin:factory",
        group=PluginLoader.GROUP,
    )
    monkeypatch.setattr(
        "registro_plugins.loader.entry_points",
        lambda *, group: (fake,) if group == PluginLoader.GROUP else (),
    )

    bus = EventBus()
    registry = PluginRegistry()
    loader = PluginLoader(registry, bus)
    try:
        loader.load_all()
        cmds = registry.all_commands()
        assert any(c.id == "tests.sample.hi" for c in cmds)
        manifests = [m.id for m in registry.manifests()]
        assert "tests.sample" in manifests
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_load_entry_point_wraps_factory_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = EntryPoint(
        name="bad",
        value="tests.fixtures.sample_plugin:does_not_exist",
        group=PluginLoader.GROUP,
    )
    monkeypatch.setattr(
        "registro_plugins.loader.entry_points",
        lambda *, group: (bad,) if group == PluginLoader.GROUP else (),
    )

    bus = EventBus()
    try:
        loader = PluginLoader(PluginRegistry(), bus)
        with pytest.raises(PluginLoadError):
            loader.load_entry_point(bad)
    finally:
        await bus.close()
