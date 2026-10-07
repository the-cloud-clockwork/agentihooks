"""Bundle and profile-chain resolution from ``state.json``."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import yaml

BUILT_IN_PROFILES = Path(__file__).resolve().parents[2] / "profiles"
PACKAGE_ROLES = BUILT_IN_PROFILES / "package" / "roles"
PACKAGE_PREFIX = "package:"


def state_path() -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "state.json"


def read_state() -> dict:
    try:
        path = state_path()
        if path.exists():
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def bundle_path(state: dict) -> Path | None:
    bp = (state.get("bundle") or {}).get("path")
    if bp:
        path = Path(bp).expanduser()
        if path.is_dir():
            return path
    return None


def active_profile(state: dict) -> str | None:
    from hooks.targets import global_record

    return os.environ.get("AGENTIHOOKS_PROFILE") or global_record(state).get("profile") or None


def linked_profiles(state: dict) -> dict[str, Path]:
    linked = {}
    for entry in state.get("linked_profiles", []) or []:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("path"):
            continue
        path = Path(entry["path"]).expanduser()
        if path.is_dir():
            linked[str(entry["name"])] = path
    return linked


def parents(path: Path) -> list[str]:
    manifest = path / "profile.yml"
    data = yaml.safe_load(manifest.read_text()) or {} if manifest.is_file() else {}
    return data.get("extends", [])


def overlays(path: Path) -> list[str]:
    manifest = path / "profile.yml"
    data = yaml.safe_load(manifest.read_text()) or {} if manifest.is_file() else {}
    return data.get("allowedOverlays", [])


def inherited(profile_dirs: list[tuple[str, Path]]) -> set[str]:
    return {parent for _, path in profile_dirs for parent in parents(path)}


def expand_profiles(names: list[str], resolve: Callable[[str], Path | None]) -> list[str]:
    out = []
    seen = set()
    visiting = []

    def visit(name):
        if name in visiting:
            raise ValueError(f"Profile inheritance cycle: {' -> '.join(visiting + [name])}")
        if name in seen:
            return
        path = resolve(name)
        if path is None:
            raise ValueError(f"Profile '{name}' not found in inheritance chain: {' -> '.join(visiting + [name])}")
        visiting.append(name)
        for parent in parents(path):
            visit(parent)
        visiting.pop()
        seen.add(name)
        out.append(name)

    for name in names:
        visit(name)
    return out


def profile_candidates(
    bundle: Path | None, profile_csv: str | None, linked: dict[str, Path]
) -> list[tuple[str, list[Path]]]:
    """Each chained profile name with its candidate dirs, highest priority first."""

    def candidates(name):
        if name.startswith(PACKAGE_PREFIX):
            return [PACKAGE_ROLES / name.removeprefix(PACKAGE_PREFIX)]
        paths = [BUILT_IN_PROFILES / name]
        if bundle is not None:
            paths.append(bundle / "profiles" / name)
        if name in linked:
            paths.append(linked[name])
        paths.append(PACKAGE_ROLES / name)
        return paths

    def resolve(name):
        return next((path for path in candidates(name) if path.is_dir()), None)

    names = [part.strip() for part in (profile_csv or "").split(",") if part.strip()]
    found = [name for name in names if resolve(name) is not None]
    expanded = expand_profiles(found, resolve)
    return [(name, candidates(name)) for name in expanded + [name for name in names if name not in found]]


def profile_dirs(bundle: Path | None, profile_csv: str | None, linked: dict[str, Path]) -> list[tuple[str, Path]]:
    """The chain's resolved profile dirs: the first existing candidate per name."""
    out = []
    for name, candidates in profile_candidates(bundle, profile_csv, linked):
        found = next((path for path in candidates if path.is_dir()), None)
        if found is not None:
            out.append((name, found))
    return out


def rendered_dirs(bundle: Path | None, profile_csv: str, linked: dict[str, Path]) -> list[tuple[str, Path]]:
    """The chain a profile home renders: the profile's dirs plus every overlay the chain declares."""
    dirs = profile_dirs(bundle, profile_csv, linked)
    declared = [overlay for _, path in dirs for overlay in overlays(path)]
    return profile_dirs(bundle, ",".join([profile_csv, *declared]), linked) if declared else dirs
