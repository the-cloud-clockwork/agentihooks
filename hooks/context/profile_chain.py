"""Bundle and profile-chain resolution from ``state.json``."""

from __future__ import annotations

import json
from pathlib import Path

BUILT_IN_PROFILES = Path(__file__).resolve().parents[2] / "profiles"


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

    return global_record(state).get("profile") or None


def linked_profiles(state: dict) -> dict[str, Path]:
    linked = {}
    for entry in state.get("linked_profiles", []) or []:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("path"):
            continue
        path = Path(entry["path"]).expanduser()
        if path.is_dir():
            linked[str(entry["name"])] = path
    return linked


def profile_candidates(
    bundle: Path | None, profile_csv: str | None, linked: dict[str, Path]
) -> list[tuple[str, list[Path]]]:
    """Each chained profile name with its candidate dirs, highest priority first."""
    out = []
    for name in (part.strip() for part in (profile_csv or "").split(",")):
        if not name:
            continue
        candidates = [BUILT_IN_PROFILES / name]
        if bundle is not None:
            candidates.append(bundle / "profiles" / name)
        if name in linked:
            candidates.append(linked[name])
        out.append((name, candidates))
    return out


def profile_dirs(bundle: Path | None, profile_csv: str | None, linked: dict[str, Path]) -> list[tuple[str, Path]]:
    """The chain's resolved profile dirs: the first existing candidate per name."""
    out = []
    for name, candidates in profile_candidates(bundle, profile_csv, linked):
        found = next((path for path in candidates if path.is_dir()), None)
        if found is not None:
            out.append((name, found))
    return out
