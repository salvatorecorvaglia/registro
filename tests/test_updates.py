"""Offline tests for release filtering and manager-owned upgrades."""

import io
import json
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from registro_cli.main import app as cli_app
from registro_core import updates
from registro_tui.app import RegistroApp


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(updates.sys, "prefix", str(tmp_path / "tools" / updates.PACKAGE))
    monkeypatch.setattr(
        updates,
        "distribution",
        lambda name: SimpleNamespace(version="1.1.0", read_text=lambda file: None),
    )
    monkeypatch.setattr(updates.shutil, "which", lambda name: f"/bin/{name}")


def response(monkeypatch, releases):
    opened = Mock(
        side_effect=lambda *a, **kw: io.BytesIO(json.dumps({"releases": releases}).encode())
    )
    monkeypatch.setattr(updates.urllib.request, "urlopen", opened)
    return opened


def test_compatible_stable_releases_and_cache(monkeypatch):
    opened = response(
        monkeypatch,
        {
            "1.0.0": [{}],
            "1.2.0": [{}],
            "2.0.0rc1": [{}],
            "3.0.0": [{"yanked": True}],
            "4.0.0": [{"requires_python": ">=99"}],
            "not-a-version": [{}],
            "5.0.0": [],
            "6.0.0": [{"requires_python": "invalid"}],
        },
    )
    assert updates.check_update(cached=True) == "1.2.0"
    assert updates.check_update(cached=True) == "1.2.0"
    assert opened.call_count == 1
    updates.check_update()
    assert opened.call_count == 2


def test_no_newer_release(monkeypatch):
    response(monkeypatch, {"1.0.0": [{}], "1.1.0": [{}]})
    assert updates.check_update() is None


def test_empty_oversized_and_offline(monkeypatch):
    response(monkeypatch, {})
    with pytest.raises(ValueError, match="No compatible"):
        updates.check_update()
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda *a, **kw: io.BytesIO(b" " * (updates.MAX_RESPONSE + 1)),
    )
    with pytest.raises(ValueError, match="too large"):
        updates.check_update()
    monkeypatch.setattr(updates.urllib.request, "urlopen", Mock(side_effect=OSError("offline")))
    with pytest.raises(OSError, match="offline"):
        updates.check_update()


def test_stale_cache_refreshed(monkeypatch, tmp_path):
    cache = tmp_path / "cache" / updates.COMMAND / "update.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"checked_at": time.time() - 90000, "latest": "9.0.0"}))
    response(monkeypatch, {"1.2.0": [{}]})
    assert updates.check_update(cached=True) == "1.2.0"


def receipt(tmp_path, manager, name=None):
    prefix = tmp_path / "tools" / updates.PACKAGE
    prefix.mkdir(parents=True)
    name = name or updates.PACKAGE
    if manager == "uv":
        (prefix / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{name = "{name}", extras = ["all"]}}]\n'
        )
    else:
        (prefix / "pipx_metadata.json").write_text(json.dumps({"main_package": {"package": name}}))
    return prefix


@pytest.mark.parametrize("manager", ["uv", "pipx"])
def test_manager_and_original_environment(tmp_path, manager):
    prefix = receipt(tmp_path, manager)
    plan = updates.upgrade_plan()
    assert plan.argv == (
        ["/bin/uv", "tool", "upgrade", updates.PACKAGE]
        if manager == "uv"
        else ["/bin/pipx", "upgrade", updates.PACKAGE]
    )
    key = "UV_TOOL_DIR" if manager == "uv" else "PIPX_HOME"
    assert plan.environment[key] == str(prefix.parent if manager == "uv" else prefix.parent.parent)
    assert updates.PACKAGE in plan.display


@pytest.mark.parametrize("manager", ["uv", "pipx"])
def test_foreign_environment_refused(tmp_path, manager):
    receipt(tmp_path, manager, "some-other-tool")
    with pytest.raises(ValueError, match="does not belong"):
        updates.upgrade_plan()


@pytest.mark.parametrize("manager", ["uv", "pipx"])
def test_missing_manager_refused(monkeypatch, tmp_path, manager):
    receipt(tmp_path, manager)
    monkeypatch.setattr(updates.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="not on PATH"):
        updates.upgrade_plan()


def test_unknown_and_source_installs_refused(monkeypatch):
    with pytest.raises(ValueError, match="unknown"):
        updates.upgrade_plan()
    monkeypatch.setattr(
        updates,
        "distribution",
        lambda name: SimpleNamespace(
            read_text=lambda file: json.dumps({"dir_info": {"editable": True}})
        ),
    )
    with pytest.raises(ValueError, match="Local/VCS"):
        updates.upgrade_plan()
    monkeypatch.setattr(updates, "distribution", Mock(side_effect=updates.PackageNotFoundError))
    with pytest.raises(ValueError, match="Source checkout"):
        updates.upgrade_plan()
    assert updates.current_version() == updates.FALLBACK_VERSION


def test_upgrade_requires_confirmation_and_dry_run_never_executes(monkeypatch, tmp_path):
    receipt(tmp_path, "uv")
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(updates.subprocess, "run", run)
    monkeypatch.setattr(updates.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert updates.upgrade(dry_run=True) == 0
    run.assert_not_called()
    with pytest.raises(ValueError, match="--yes"):
        updates.upgrade()
    run.assert_not_called()
    assert updates.upgrade(yes=True) == 0
    assert run.call_args.kwargs["check"] is False
    assert run.call_args.kwargs["env"]["UV_TOOL_DIR"].endswith("tools")
    run.return_value = subprocess.CompletedProcess([], 3)
    assert updates.upgrade(yes=True) == 3


def test_interactive_cancel_and_confirm(monkeypatch, tmp_path):
    receipt(tmp_path, "uv")
    monkeypatch.setattr(updates.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(updates.subprocess, "run", run)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert updates.upgrade() == 0
    run.assert_not_called()
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert updates.upgrade() == 0
    run.assert_called_once()


def test_cli_check_and_upgrade(monkeypatch):
    monkeypatch.setattr(updates, "check_update", lambda: "1.2.0")
    result = CliRunner().invoke(cli_app, ["check-updates"])
    assert result.exit_code == 0
    assert "1.2.0" in result.stdout
    monkeypatch.setattr(updates, "check_update", Mock(side_effect=OSError("offline")))
    assert CliRunner().invoke(cli_app, ["check-updates"]).exit_code == 1
    monkeypatch.setattr(updates, "upgrade", lambda **kwargs: 3)
    assert CliRunner().invoke(cli_app, ["upgrade", "--yes"]).exit_code == 3
    monkeypatch.setattr(updates, "upgrade", Mock(side_effect=ValueError("unknown manager")))
    assert CliRunner().invoke(cli_app, ["upgrade", "--dry-run"]).exit_code == 1


@pytest.mark.asyncio
async def test_tui_notifies_once_and_offline_is_silent(monkeypatch):
    app = SimpleNamespace(notify=Mock())
    monkeypatch.setattr(updates, "check_update", lambda **kwargs: "1.2.0")
    await RegistroApp._check_for_updates(app)
    assert "registro upgrade" in app.notify.call_args.args[0]
    app.notify.reset_mock()
    monkeypatch.setattr(updates, "check_update", Mock(side_effect=OSError("offline")))
    await RegistroApp._check_for_updates(app)
    app.notify.assert_not_called()
