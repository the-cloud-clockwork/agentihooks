"""Which overlays a swarm agent wears: the task's own list, else its base role's default in the swarm config."""

import subprocess

import yaml

from hooks.context import profile_chain

ROLES = ("master", "engineer", "planner", "qa", "cicd")
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


def chosen(profile: str, task: dict, defaults: dict) -> tuple[str, ...]:
    if "overlays" not in task and not defaults:
        return ()
    installer = _install()
    dirs = installer._resolve_profile_chain(profile)
    names = task["overlays"] if "overlays" in task else defaults.get(profile_chain.role(dirs), [])
    return tuple(profile_chain.worn(dirs, list(names), installer._resolve_profile_dir))


def available() -> list[dict]:
    bundle = _install()._get_bundle_path()
    found = []
    for manifest in sorted((bundle / "profiles").glob("*/profile.yml")) if bundle else []:
        try:
            roles = profile_chain.wears(manifest.parent)
        except (AttributeError, ValueError, yaml.YAMLError):
            continue
        if roles:
            found.append({"name": manifest.parent.name, "wears": roles})
    return found


def revision() -> str:
    bundle = _install()._get_bundle_path()
    if bundle is None:
        return ""
    done = subprocess.run(["git", "-C", str(bundle), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    return done.stdout.strip() if done.returncode == 0 else ""
