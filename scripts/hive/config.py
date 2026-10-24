"""A hive's configuration named without values or host paths, so any two hives compare."""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.hive import registry

if TYPE_CHECKING:
    from redis import Redis

TOKEN_PREFIXES = ("AH_CC_TOKEN_", "AH_CX_TOKEN_")
HIVE_ENV = "hive.env"
SECTIONS = ("accounts", "settings", "bundle", "profiles", "rules", "skills", "mcp", "deps", "repos")
TTL_SECONDS = 900
REFRESH_SECONDS = 60


def snapshot(environ: Mapping[str, str], home: Path, state: Path, repos: list[str]) -> dict[str, list[str]]:
    from scripts.hive.daemon import origin

    installed = _installed(state / "state.json")
    bundle = installed.get("bundle", {}).get("path")
    identity = origin(Path(bundle)) if bundle else ""
    claude = home / ".claude"
    return {
        "accounts": sorted(name for name, value in environ.items() if name.startswith(TOKEN_PREFIXES) and value),
        "settings": _settings(state),
        "bundle": [identity] if identity else [],
        "profiles": sorted(profile["name"] for profile in installed.get("linked_profiles", [])),
        "rules": _names(claude / "rules"),
        "skills": _names(claude / "skills"),
        "mcp": sorted(installed.get("managed_mcp_servers", [])),
        "deps": _deps(),
        "repos": sorted(repo for repo in repos if not repo.startswith("/")),
    }


def _installed(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _settings(state: Path) -> list[str]:
    from hooks.context.credential_guard import dotenv_keys

    files = sorted(state.glob("*.env")) if state.is_dir() else []
    return sorted({key for path in files if path.name != HIVE_ENV for key in dotenv_keys(path)})


def _names(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def _deps() -> list[str]:
    from scripts import deps_preflight

    path = deps_preflight.manifest_path()
    try:
        declared = deps_preflight.parse(json.loads(path.read_text())) if path else []
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []
    return sorted(dep.id for dep in declared if deps_preflight.satisfied(dep))


def differences(base: dict, other: dict) -> list[str]:
    lines = []
    for section in SECTIONS:
        mine, theirs = set(base[section]), set(other[section])
        lines += [f"{section} missing {name}" for name in sorted(mine - theirs)]
        lines += [f"{section} extra {name}" for name in sorted(theirs - mine)]
    return lines


def summary(found: dict) -> str:
    return " ".join(f"{section}={len(found.get(section, []))}" for section in SECTIONS)


def _key(hive_id: str) -> str:
    return f"{registry.key(hive_id)}:config"


def publish(redis: "Redis", local_id: str, repos: list[str]) -> None:
    """Dependency checks run commands, so a hive refreshes its snapshot only shortly before the last one expires."""
    if redis.ttl(_key(local_id)) > REFRESH_SECONDS:
        return
    found = snapshot(os.environ, Path.home(), registry.home(), repos)
    redis.set(_key(local_id), json.dumps(found), ex=TTL_SECONDS)


def read(redis: "Redis", hive_id: str) -> dict | None:
    raw = redis.get(_key(hive_id))
    return json.loads(raw) if raw else None
