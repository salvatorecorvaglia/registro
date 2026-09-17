"""Vim keymap actually registers its extra bindings at app init."""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from registro_core.config import RegistroConfig, UIConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402

# Vim-only keys that DEFAULT_BINDINGS does not include.
_VIM_ONLY = {"colon", "g", "shift+g"}


@pytest.mark.asyncio
async def test_default_keymap_lacks_vim_bindings() -> None:
    app = RegistroApp(
        config=RegistroConfig(ui=UIConfig(keymap="default")),
        initial_dsn=Dsn.parse("sqlite:///:memory:"),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        keys = set(app._bindings.key_to_bindings.keys())
        assert _VIM_ONLY.isdisjoint(keys), (
            f"vim-only bindings leaked into default keymap: {keys & _VIM_ONLY}"
        )


@pytest.mark.asyncio
async def test_vim_keymap_registers_extra_bindings() -> None:
    app = RegistroApp(
        config=RegistroConfig(ui=UIConfig(keymap="vim")),
        initial_dsn=Dsn.parse("sqlite:///:memory:"),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        keys = set(app._bindings.key_to_bindings.keys())
        missing = _VIM_ONLY - keys
        assert not missing, f"vim keymap did not register: {missing}"
