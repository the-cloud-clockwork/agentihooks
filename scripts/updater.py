"""Self-update: resolve how this copy was installed, then upgrade that copy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path

PACKAGE = "agentihooks"
PYPI_JSON = f"https://pypi.org/pypi/{PACKAGE}/json"


def installed_version() -> str:
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return "unknown"


def latest_version(timeout: float = 10.0) -> str | None:
    try:
        with urllib.request.urlopen(PYPI_JSON, timeout=timeout) as resp:
            return str(json.load(resp)["info"]["version"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None


def _uv_tools_dir() -> Path:
    override = os.environ.get("UV_TOOL_DIR")
    if override:
        return Path(override).expanduser()
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data).expanduser() / "uv" / "tools"


def _pipx_venvs_dir() -> Path:
    override = os.environ.get("PIPX_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".local" / "pipx"
    return base / "venvs"


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def is_editable() -> bool:
    try:
        dist = distribution(PACKAGE)
    except PackageNotFoundError:
        return False
    try:
        raw = dist.read_text("direct_url.json")
    except (OSError, ValueError):
        return False
    if not raw:
        return False
    try:
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except ValueError:
        return False


def install_mode(source_checkout: bool = False) -> str:
    """Which installation this process is running out of.

    Decides the upgrade command. Reading sys.prefix rather than looking for a
    tool on PATH is the whole point: uv being installed says nothing about
    whether *this* copy came from `uv tool install`.
    """
    if source_checkout or is_editable():
        return "editable"
    prefix = Path(sys.prefix)
    if _under(prefix, _uv_tools_dir()):
        return "uv-tool"
    if _under(prefix, _pipx_venvs_dir()):
        return "pipx"
    return "pip"


def upgrade_command(mode: str, index_url: str | None = None) -> list[str]:
    if mode == "uv-tool":
        cmd = ["uv", "tool", "install", "--force", PACKAGE]
        if index_url:
            cmd += ["--index-url", index_url]
        return cmd
    if mode == "pipx":
        return ["pipx", "upgrade", PACKAGE]
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]
    if index_url:
        cmd += ["--index-url", index_url]
    return cmd


def version_after_upgrade() -> str:
    """Re-read the version from a fresh interpreter.

    importlib.metadata in this process was populated before the upgrade
    replaced the files on disk, so asking it again reports the old number.
    """
    code = f"import importlib.metadata as m; print(m.version({PACKAGE!r}))"
    try:
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def run_update(
    *,
    check_only: bool = False,
    index_url: str | None = None,
    source_checkout: bool = False,
    echo=print,
) -> int:
    current = installed_version()
    mode = install_mode(source_checkout)
    echo(f"{PACKAGE} {current} (installed via: {mode})")

    if mode == "editable":
        echo("Editable install — update the source checkout with git pull.")
        return 0

    latest = None if index_url else latest_version()
    if latest:
        echo(f"Latest on PyPI: {latest}")
        if latest == current:
            echo("Already up to date.")
            return 0
    elif not index_url:
        echo("Could not reach PyPI; upgrading anyway.")

    cmd = upgrade_command(mode, index_url)
    if check_only:
        echo(f"Update available. Would run: {' '.join(cmd)}")
        return 0

    echo(f"Running: {' '.join(cmd)}")
    try:
        done = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        echo(f"Upgrade tool not found: {cmd[0]}")
        return 1
    if done.returncode != 0:
        echo(f"Update failed:\n{done.stderr.strip()}")
        return 1

    new = version_after_upgrade()
    if new != current and new != "unknown":
        echo(f"Updated: {current} -> {new}")
    else:
        echo(f"Upgrade command succeeded but version is still {current}.")
    return 0
