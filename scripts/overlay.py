"""Overlay system — shared helpers for mid-session profile chaining.

An overlay injects a profile's CLAUDE.md + rules content into the current
session via the hook system (UserPromptSubmit → inject_context). The base
profile remains installed normally; overlays are layered on top at runtime.

State file: ~/.agentihooks/active_overlays.json

Public API:
    overlay_list(base_profile_dir)     → list of allowed overlay names
    overlay_add(name, base_profile_dir, added_by)  → rendered overlay dict
    overlay_remove(name)               → bool
    overlay_refresh(name, base_profile_dir) → rendered overlay dict | None
    overlay_clear()                    → int (count removed)
    get_active_overlays()              → list[dict]
    get_overlay_content()              → str | None (formatted injection block)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from hooks.common import log
from hooks.config import AGENTIHOOKS_HOME

OVERLAYS_FILE = Path(AGENTIHOOKS_HOME) / "active_overlays.json"


def _load_overlays() -> dict:
    if OVERLAYS_FILE.exists():
        try:
            return json.loads(OVERLAYS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {"base_profile": "", "overlays": []}
    return {"base_profile": "", "overlays": []}


def _save_overlays(data: dict) -> None:
    OVERLAYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERLAYS_FILE.write_text(json.dumps(data, indent=2))


def _render_profile_content(profile_dir: Path) -> str:
    """Render a profile's CLAUDE.md + rules/*.md into a single text block."""
    parts = []

    claude_md = profile_dir / "CLAUDE.md"
    if claude_md.exists():
        parts.append(claude_md.read_text().strip())

    rules_dir = profile_dir / ".claude" / "rules"
    if rules_dir.is_dir():
        for rule_file in sorted(rules_dir.glob("*.md")):
            content = rule_file.read_text().strip()
            if content:
                parts.append(f"### {rule_file.stem}\n\n{content}")

    return "\n\n---\n\n".join(parts)


def _get_allowed_overlays(base_profile_dir: Path) -> list[str]:
    """Read allowedOverlays from base profile's profile.yml."""
    profile_yml = base_profile_dir / "profile.yml"
    if not profile_yml.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(profile_yml.read_text())
        return data.get("allowedOverlays", []) or []
    except Exception:
        # Fallback: parse YAML manually for the simple list case
        lines = profile_yml.read_text().splitlines()
        in_section = False
        allowed = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("allowedOverlays:"):
                in_section = True
                continue
            if in_section:
                if stripped.startswith("- "):
                    allowed.append(stripped[2:].strip())
                elif stripped and not stripped.startswith("#"):
                    break
        return allowed


def _resolve_profile_dir(name: str) -> Path | None:
    """Resolve a profile name to its directory. Reuses install.py logic."""
    try:
        from scripts.install import _resolve_profile_dir as _resolve

        return _resolve(name)
    except ImportError:
        pass

    # Fallback: check common locations
    agentihooks_root = Path(AGENTIHOOKS_HOME).parent
    for search in [
        Path(AGENTIHOOKS_HOME) / "profiles" / name,
        agentihooks_root / "profiles" / name,
    ]:
        if search.is_dir():
            return search

    # Check bundle
    state_file = Path(AGENTIHOOKS_HOME) / "state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            bundle_path = state.get("bundle", {}).get("path", "")
            if bundle_path:
                candidate = Path(bundle_path) / "profiles" / name
                if candidate.is_dir():
                    return candidate
        except Exception:
            pass

    return None


def _resolve_base_profile_dir() -> Path | None:
    """Resolve the currently installed base profile directory."""
    state_file = Path(AGENTIHOOKS_HOME) / "state.json"
    if not state_file.exists():
        return None
    try:
        state = json.loads(state_file.read_text())
        profile_name = state.get("targets", {}).get("global", {}).get("profile", "")
        if not profile_name:
            return None
        # For chained profiles, use the first one as the "base"
        base_name = profile_name.split(",")[0].strip()
        return _resolve_profile_dir(base_name)
    except Exception:
        return None


def overlay_list(base_profile_dir: Path | None = None) -> list[dict]:
    """List all overlays allowed by the base profile."""
    if base_profile_dir is None:
        base_profile_dir = _resolve_base_profile_dir()
    if base_profile_dir is None:
        return []

    allowed = _get_allowed_overlays(base_profile_dir)
    result = []
    for name in allowed:
        pdir = _resolve_profile_dir(name)
        desc = ""
        if pdir:
            profile_yml = pdir / "profile.yml"
            if profile_yml.exists():
                for line in profile_yml.read_text().splitlines():
                    if line.strip().startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                        break
        result.append({"name": name, "available": pdir is not None, "description": desc})
    return result


def overlay_add(name: str, base_profile_dir: Path | None = None, added_by: str = "cli") -> dict:
    """Add an overlay. Returns the overlay entry dict or error dict."""
    if base_profile_dir is None:
        base_profile_dir = _resolve_base_profile_dir()
    if base_profile_dir is None:
        return {"success": False, "error": "Cannot resolve base profile directory"}

    allowed = _get_allowed_overlays(base_profile_dir)
    if name not in allowed:
        return {
            "success": False,
            "error": f"'{name}' not in allowedOverlays. Allowed: {allowed}",
        }

    overlay_dir = _resolve_profile_dir(name)
    if overlay_dir is None:
        return {"success": False, "error": f"Profile '{name}' not found on disk"}

    data = _load_overlays()
    # Check if already active
    for existing in data.get("overlays", []):
        if existing.get("name") == name:
            return {"success": False, "error": f"Overlay '{name}' already active"}

    content = _render_profile_content(overlay_dir)
    rules_dir = overlay_dir / ".claude" / "rules"
    entry = {
        "name": name,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "added_by": added_by,
        "rules_content": content,
        "rules_dir": str(rules_dir) if rules_dir.is_dir() else "",
    }

    if not data.get("base_profile"):
        data["base_profile"] = base_profile_dir.name
    data.setdefault("overlays", []).append(entry)
    _save_overlays(data)

    log("overlay_add", {"name": name, "added_by": added_by})

    # Force immediate rules re-injection for current session
    try:
        from hooks.context.broadcast import _load_sessions
        from hooks.context.context_refresh import force_rules_refresh
        sessions = _load_sessions()  # dict keyed by session_id
        if sessions:
            latest_sid = max(sessions, key=lambda sid: sessions[sid].get("registered_at", ""))
            force_rules_refresh(latest_sid, sessions[latest_sid].get("cwd", ""))
    except Exception:
        pass

    # Broadcast activation to fleet
    try:
        from hooks.config import PROFILE_BROADCAST_ENABLED
        if PROFILE_BROADCAST_ENABLED:
            import os

            from hooks.context.broadcast import create_broadcast
            agent = os.getenv("AGENTICORE_AGENT_NAME", os.getenv("USER", "unknown"))
            create_broadcast(
                message=f"Profile **{name}** activated on {agent}",
                severity="info",
                ttl_seconds=300,
                source="overlay-system",
                channel="brain",
            )
    except Exception:
        pass

    return {"success": True, "overlay": entry}


def overlay_remove(name: str) -> dict:
    """Remove an active overlay."""
    data = _load_overlays()
    overlays = data.get("overlays", [])
    new_overlays = [o for o in overlays if o.get("name") != name]

    if len(new_overlays) == len(overlays):
        return {"success": False, "error": f"Overlay '{name}' not active"}

    data["overlays"] = new_overlays
    _save_overlays(data)
    log("overlay_remove", {"name": name})

    # Broadcast deactivation to fleet
    try:
        from hooks.config import PROFILE_BROADCAST_ENABLED
        if PROFILE_BROADCAST_ENABLED:
            import os

            from hooks.context.broadcast import create_broadcast
            agent = os.getenv("AGENTICORE_AGENT_NAME", os.getenv("USER", "unknown"))
            create_broadcast(
                message=f"Profile **{name}** deactivated on {agent}",
                severity="info",
                ttl_seconds=300,
                source="overlay-system",
                channel="brain",
            )
    except Exception:
        pass

    return {"success": True, "removed": name}


def overlay_refresh(name: str, base_profile_dir: Path | None = None) -> dict:
    """Re-render an active overlay from disk."""
    data = _load_overlays()
    for entry in data.get("overlays", []):
        if entry.get("name") == name:
            overlay_dir = _resolve_profile_dir(name)
            if overlay_dir is None:
                return {"success": False, "error": f"Profile '{name}' not found on disk"}
            entry["rules_content"] = _render_profile_content(overlay_dir)
            entry["added_at"] = datetime.now(timezone.utc).isoformat()
            _save_overlays(data)
            log("overlay_refresh", {"name": name})
            return {"success": True, "overlay": entry}
    return {"success": False, "error": f"Overlay '{name}' not active"}


def overlay_clear() -> dict:
    """Remove all active overlays."""
    data = _load_overlays()
    count = len(data.get("overlays", []))
    data["overlays"] = []
    _save_overlays(data)
    log("overlay_clear", {"count": count})
    return {"success": True, "removed_count": count}


def get_active_overlays() -> list[dict]:
    """Return list of active overlay entries."""
    return _load_overlays().get("overlays", [])


def get_overlay_content() -> str | None:
    """Return formatted injection block for all active overlays, or None if empty."""
    overlays = get_active_overlays()
    if not overlays:
        return None

    blocks = []
    for o in overlays:
        name = o.get("name", "unknown")
        content = o.get("rules_content", "")
        if content:
            blocks.append(f"=== OVERLAY ACTIVE: {name} ===\n{content}\n=== END OVERLAY: {name} ===")

    return "\n\n".join(blocks) if blocks else None
