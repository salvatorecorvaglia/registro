"""Release discovery and upgrades through the owning tool manager."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

PACKAGE = "registro-db"
COMMAND = "registro"
FALLBACK_VERSION = "1.2.0"
MAX_RESPONSE = 2 * 1024 * 1024


def current_version() -> str:
    try:
        return distribution(PACKAGE).version
    except PackageNotFoundError:
        return FALLBACK_VERSION


def check_update(*, cached: bool = False) -> str | None:
    """Return a newer stable, non-yanked release compatible with this Python."""
    cache = (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / COMMAND
        / "update.json"
    )
    python_tag = ".".join(map(str, sys.version_info[:3]))
    latest = None
    if cached:
        try:
            saved = json.loads(cache.read_text())
            if 0 <= time.time() - saved["checked_at"] < 86400 and saved.get("python") == python_tag:
                latest = str(Version(saved["latest"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if latest is None:
        request = urllib.request.Request(
            f"https://pypi.org/pypi/{PACKAGE}/json",
            headers={"User-Agent": f"{COMMAND}/{current_version()} update-check"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = response.read(MAX_RESPONSE + 1)
        if len(payload) > MAX_RESPONSE:
            raise ValueError("Update response is too large")
        releases = json.loads(payload)["releases"]
        candidates = []
        python = python_tag
        for name, files in releases.items():
            try:
                version = Version(name)
                compatible = any(
                    not file.get("yanked", False)
                    and python in SpecifierSet(file.get("requires_python") or "")
                    for file in files
                )
                if not version.is_prerelease and not version.is_devrelease and compatible:
                    candidates.append(version)
            except (InvalidVersion, ValueError, TypeError):
                continue
        if not candidates:
            raise ValueError("No compatible stable releases found")
        latest = str(max(candidates))
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps({"checked_at": time.time(), "latest": latest, "python": python_tag})
            )
            temporary.replace(cache)
        except OSError:
            pass
    return latest if Version(latest) > Version(current_version()) else None


@dataclass(frozen=True)
class UpgradePlan:
    argv: list[str]
    environment: dict[str, str]

    @property
    def display(self) -> str:
        return shlex.join(self.argv)


def upgrade_plan() -> UpgradePlan:
    """Only uv/pipx-owned tool environments may be changed automatically."""
    prefix = Path(sys.prefix)
    try:
        installed = distribution(PACKAGE)
        direct = json.loads(installed.read_text("direct_url.json") or "{}")
    except PackageNotFoundError as exc:
        raise ValueError("Source checkout: update it with Git and sync dependencies") from exc
    if direct:
        raise ValueError("Local/VCS installation: update it with its original installation method")
    receipt = prefix / "uv-receipt.toml"
    metadata = prefix / "pipx_metadata.json"
    environment = dict(os.environ)
    if receipt.is_file():
        data = tomllib.loads(receipt.read_text())
        names = [
            item.get("name", "").replace("_", "-")
            for item in data.get("tool", {}).get("requirements", [])
        ]
        if PACKAGE not in names:
            raise ValueError("This uv environment does not belong to this tool")
        executable = shutil.which("uv")
        if not executable:
            raise ValueError("uv is not on PATH")
        environment["UV_TOOL_DIR"] = str(prefix.parent)
        return UpgradePlan([executable, "tool", "upgrade", PACKAGE], environment)
    if metadata.is_file():
        data = json.loads(metadata.read_text())
        name = data.get("main_package", {}).get("package", "").replace("_", "-")
        if name != PACKAGE:
            raise ValueError("This pipx environment does not belong to this tool")
        executable = shutil.which("pipx")
        if not executable:
            raise ValueError("pipx is not on PATH")
        environment["PIPX_HOME"] = str(prefix.parent.parent)
        return UpgradePlan([executable, "upgrade", PACKAGE], environment)
    raise ValueError(
        "Installation manager is unknown; use the original pip/uv/Git/container "
        "installation method. No environment was changed."
    )


def upgrade(*, yes: bool = False, dry_run: bool = False) -> int:
    plan = upgrade_plan()
    print(f"Upgrade command: {plan.display}")
    if dry_run:
        return 0
    if not yes:
        if not sys.stdin.isatty():
            raise ValueError("Use --yes to confirm an upgrade without an interactive terminal")
        if input("Upgrade this tool now? [y/N] ").strip().lower() not in {"y", "yes"}:
            return 0
    result = subprocess.run(plan.argv, env=plan.environment, check=False)
    if result.returncode == 0:
        print(f"Upgrade completed. Restart {COMMAND} to use the new version.")
    return result.returncode
