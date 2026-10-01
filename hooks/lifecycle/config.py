import json
from dataclasses import fields
from pathlib import Path

from hooks.lifecycle.model import Root

BASE_FILE = Path(__file__).resolve().parents[2] / "profiles" / "_base" / "lifecycle.json"
KINDS = {"worktrees", "scratch", "ttl", "archive"}


def _entries(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    roots = data.get("roots", []) if isinstance(data, dict) else []
    return [entry for entry in roots if isinstance(entry, dict) and entry.get("id")]


def _overlay_files() -> list[Path]:
    from hooks.config import AGENTIHOOKS_HOME
    from hooks.context import profile_chain

    state = profile_chain.read_state()
    bundle = profile_chain.bundle_path(state)
    files = [bundle / "lifecycle.json"] if bundle else []
    profile = profile_chain.active_profile(state)
    if profile:
        linked = profile_chain.linked_profiles(state)
        files += [d / "lifecycle.json" for _name, d in profile_chain.profile_dirs(bundle, profile, linked)]
    return files + [AGENTIHOOKS_HOME / "lifecycle.json"]


def _root(entry: dict) -> Root | None:
    known = {f.name for f in fields(Root)}
    values = {key: value for key, value in entry.items() if key in known}
    if values.get("kind") not in KINDS or not values.get("path"):
        return None
    values["path"] = str(Path(values["path"]).expanduser())
    values["include"] = tuple(values.get("include", ()))
    try:
        return Root(**values)
    except TypeError:
        return None


def load_roots(files: list[Path] | None = None) -> list[Root]:
    merged: dict[str, dict] = {}
    for path in [BASE_FILE, *(_overlay_files() if files is None else files)]:
        for entry in _entries(path):
            merged[entry["id"]] = {**merged.get(entry["id"], {}), **entry}
    roots = []
    for entry in merged.values():
        if entry.get("enabled", True) is False:
            continue
        root = _root(entry)
        if root:
            roots.append(root)
    return roots
