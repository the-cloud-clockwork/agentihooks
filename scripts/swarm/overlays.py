"""Which overlays a swarm agent wears: the task's own list, else its base role's default in the swarm config."""

import subprocess
import sys
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

import yaml

from hooks.context import profile_chain
from scripts.profiles import bundle_copy

ROLES = ("master", "engineer", "planner", "qa", "cicd", "documentation")
KEY = "overlays-"


def _install():
    from scripts.targets._common import _install_module

    return _install_module()


def setting(key: str, value: str, current: dict, offered: list[dict]) -> dict:
    role = key.removeprefix(KEY)
    if role not in ROLES:
        raise ValueError(f"overlays are set per base role: {', '.join(KEY + r for r in ROLES)}")
    names = list(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))
    if len(names) > profile_chain.OVERLAY_CAP:
        raise ValueError(
            f"a role wears at most {profile_chain.OVERLAY_CAP} overlays; {len(names)} were set: {', '.join(names)}"
        )
    wears = {overlay["name"]: overlay["wears"] for overlay in offered}
    for name in names:
        if name not in wears:
            raise ValueError(f"overlay {name} not found")
        if role not in wears[name]:
            raise ValueError(f"overlay {name} does not wear the {role} role")
    return {r: worn for r, worn in {**current, role: names}.items() if worn}


def chosen(
    profile: str, task: dict[str, Any], defaults: dict[str, list[str]], hosted: Collection[str]
) -> tuple[str, ...]:
    """The overlays the profile wears, or none when this host lacks the profile or an overlay a target hive hosts."""
    if "overlays" not in task and not defaults:
        return ()
    installer = _install()
    resolve = installer._resolve_profile_dir
    if _remote(profile, hosted, resolve):
        return ()
    dirs = installer._resolve_profile_chain(profile)
    names: list[str] = task["overlays"] if "overlays" in task else defaults.get(profile_chain.role(dirs), [])
    if any(_remote(name, hosted, resolve) for name in names):
        return ()
    return tuple(profile_chain.worn(dirs, list(names), resolve))


def _remote(name: str, hosted: Collection[str], resolve: Callable[[str], Path | None]) -> bool:
    """A placement target hive hosts the profile and this host cannot resolve it, so that hive resolves it."""
    return name in hosted and resolve(name) is None


def available() -> list[dict]:
    found: dict[str, dict] = {}
    for _kind, bundle in _install()._bundle_roots():
        for manifest in sorted((bundle / "profiles").glob("*/profile.yml")):
            try:
                roles = profile_chain.wears(manifest.parent)
            except (AttributeError, ValueError, yaml.YAMLError):
                continue
            if roles and manifest.parent.name not in found:
                found[manifest.parent.name] = {"name": manifest.parent.name, "wears": roles}
    return sorted(found.values(), key=lambda overlay: overlay["name"])


def _head(bundle) -> str:
    done = subprocess.run(["git", "-C", str(bundle), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    return done.stdout.strip() if done.returncode == 0 else ""


def revision() -> str:
    installer = _install()
    bundle, user = installer._get_bundle_path(), installer._get_user_bundle_path()
    shared = installer._get_shared_bundle_path()
    if bundle is None:
        return ""
    root = _head(bundle)
    if not root:
        return root
    team = _head(shared) if shared is not None else ""
    if shared is not None and not team:
        print(f"WARNING: the shared bundle at {shared} has no commit to record; launching without it", file=sys.stderr)
        team = bundle_copy.UNPINNED
    mine = _head(user) if user is not None else ""
    if user is not None and not mine:
        return ""
    if shared is not None:
        return f"{root}:{team}:{mine}"
    return f"{root}:{mine}" if user is not None else root
