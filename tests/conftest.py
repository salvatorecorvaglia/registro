"""Top-level pytest config."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

from registro_adapters.registry import reset_default_registry

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_adapter_registry() -> Iterator[None]:
    """Reset the process-wide adapter registry around every test.

    The registry is a module-level singleton; clearing it before and after
    each test stops state from leaking through import order.
    """
    reset_default_registry()
    yield
    reset_default_registry()


@pytest.fixture(autouse=True)
def _isolate_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Point ``$HOME`` at a throwaway directory for every test.

    The TUI writes its query history and log file under ``~/.registro``, and
    reads its config from ``~/.config/registro``. Without this, booting the app
    in a test appended to the developer's real history database and redirected
    the rest of the session's logging into their real log file.

    The structlog configuration is restored afterwards: the app reconfigures
    logging (``force=True``) to point at the per-test log file, and that must
    not leak into the tests that follow.
    """
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)
