"""Enforcement system — drumbeat re-injection of operator-curated rules.

Parallel to broadcast.py but semantically distinct:
- No severity, no TTL, no per-session targeting.
- Global entries reach every session; local entries reach only their project.
- Permanent until operator clears (runtime) or removed in git (bundle/profile).
- Cadence-driven: each enforcement re-injects every N tool calls.

Four-source resolution (priority: local > runtime > profile > bundle):
  1. <bundle_path>/enforcements.json                          → source: "bundle"
  2. <bundle_path>/profiles/<active_profile>/enforcements.json → source: "profile"
  3. ~/.agentihooks/enforcements.json                          → source: "runtime"
  4. <project>/.agentihooks/enforcements.json                  → source: "local"

Runtime store: ~/.agentihooks/enforcements.json (mutable via CLI/MCP).
Bundle/profile stores: read-only at runtime, editable only in git.
Local store: mutable through CLI commands carrying --local.
Counter: per-session, persisted at ~/.agentihooks/enforcement_counters.json.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hooks.config import ENFORCEMENT_COUNTER_FILE, ENFORCEMENT_FILE, ENFORCEMENT_INJECTION_ENABLED


def _store_path() -> Path:
    return Path(ENFORCEMENT_FILE).expanduser()


def _counter_path() -> Path:
    return Path(ENFORCEMENT_COUNTER_FILE).expanduser()


def _load_store(path: Path | None = None) -> list[dict]:
    p = path or _store_path()
    if not p.exists() or p.stat().st_size == 0:
        return []
    try:
        data = json.loads(p.read_text())
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            entries = data.get("enforcements", [])
            return entries if isinstance(entries, list) else []
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _save_store(entries: list[dict], path: Path | None = None) -> None:
    p = path or _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"enforcements": entries}, indent=2))
    os.replace(str(tmp), str(p))


def _load_counters() -> dict:
    p = _counter_path()
    if not p.exists() or p.stat().st_size == 0:
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_counters(state: dict) -> None:
    p = _counter_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(str(tmp), str(p))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


from hooks.config import AGENTIHOOKS_HOME

_STATE_PATH = AGENTIHOOKS_HOME / "state.json"


def _read_state() -> dict:
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _get_bundle_path() -> Path | None:
    bp = _read_state().get("bundle", {}).get("path")
    if bp:
        p = Path(bp).expanduser()
        if p.is_dir():
            return p
    return None


def _get_active_profile() -> str | None:
    from hooks.targets import global_record

    return global_record(_read_state()).get("profile") or None


def _load_json_enforcements(path: Path, source: str) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            entries = data.get("enforcements", [])
            if not isinstance(entries, list):
                return []
        else:
            return []
        for e in entries:
            e["source"] = source
        return entries
    except (json.JSONDecodeError, OSError):
        return []


def _load_bundle_enforcements() -> list[dict]:
    bp = _get_bundle_path()
    if not bp:
        return []
    return _load_json_enforcements(bp / "enforcements.json", "bundle")


def _load_profile_enforcements() -> list[dict]:
    bp = _get_bundle_path()
    profile = _get_active_profile()
    if not bp or not profile:
        return []
    return _load_json_enforcements(bp / "profiles" / profile / "enforcements.json", "profile")


def _local_store_path(cwd: str | Path | None, *, create_parent: bool = False) -> Path:
    from hooks.context.project_resources import project_resource_path

    return project_resource_path("enforcements.json", cwd, create_parent=create_parent)


def _load_local_enforcements(cwd: str | Path | None) -> list[dict]:
    if cwd is None:
        return []
    try:
        return _load_json_enforcements(_local_store_path(cwd), "local")
    except ValueError:
        return []


def load_all_enforcements(cwd: str | Path | None = None) -> list[dict]:
    """Merge bundle → profile → runtime → local."""
    by_id: dict[str, dict] = {}
    for e in _load_bundle_enforcements():
        eid = e.get("id")
        if eid:
            by_id[eid] = e
    for e in _load_profile_enforcements():
        eid = e.get("id")
        if eid:
            by_id[eid] = e
    for e in _load_store():
        e["source"] = "runtime"
        eid = e.get("id")
        if eid:
            by_id[eid] = e
    for e in _load_local_enforcements(cwd):
        eid = e.get("id")
        if eid:
            by_id[eid] = e
    return list(by_id.values())


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def add_enforcement(
    message: str,
    cadence: int,
    tag: str | None = None,
    *,
    local: bool = False,
    cwd: str | Path | None = None,
) -> str | None:
    if not message or not message.strip():
        return None
    if not isinstance(cadence, int) or cadence < 1:
        return None
    enforcement_id = uuid.uuid4().hex[:8]
    entry = {
        "id": enforcement_id,
        "message": message.strip(),
        "cadence": cadence,
        "tag": tag or "",
        "created_at": _now_iso(),
    }
    store = _local_store_path(cwd, create_parent=True) if local else _store_path()
    entries = _load_store(store)
    entries.append(entry)
    _save_store(entries, store)
    return enforcement_id


def list_enforcements(*, local: bool = False, cwd: str | Path | None = None) -> list[dict]:
    if local:
        return _load_json_enforcements(_local_store_path(cwd), "local")
    return load_all_enforcements()


def clear_enforcement(
    enforcement_id: str | None = None,
    tag: str | None = None,
    *,
    local: bool = False,
    cwd: str | Path | None = None,
) -> int:
    store = _local_store_path(cwd) if local else _store_path()
    if local and not store.exists():
        return 0
    entries = _load_store(store)
    if enforcement_id is None and not tag:
        count = len(entries)
        _save_store([], store)
        return count
    if enforcement_id:
        remaining = [e for e in entries if e.get("id") != enforcement_id]
    else:
        remaining = [e for e in entries if e.get("tag") != tag]
    count = len(entries) - len(remaining)
    _save_store(remaining, store)
    return count


# ---------------------------------------------------------------------------
# Counter + due-check
# ---------------------------------------------------------------------------


def increment_and_get_count(session_id: str) -> int:
    """Increment the per-session tool-call counter and return the new value."""
    state = _load_counters()
    cur = int(state.get(session_id, 0)) + 1
    state[session_id] = cur
    _save_counters(state)
    return cur


def get_due_enforcements(tool_call_count: int, cwd: str | Path | None = None) -> list[dict]:
    """Return enforcements whose cadence divides the current count."""
    if tool_call_count <= 0:
        return []
    entries = load_all_enforcements(cwd)
    due: list[dict] = []
    for e in entries:
        cadence = int(e.get("cadence", 0) or 0)
        if cadence < 1:
            continue
        if tool_call_count % cadence == 0:
            due.append(e)
    return due


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_enforcement_banner(msg: dict) -> str:
    message = msg.get("message", "")
    tag = msg.get("tag") or ""
    enforcement_id = msg.get("id", "")
    cadence = msg.get("cadence", "")

    lines = [
        "=== ENFORCEMENT [IMPORTANT] ===",
        f"ID: {enforcement_id}  Cadence: every {cadence} tool calls",
    ]
    if tag:
        lines.append(f"Tag: {tag}")
    lines.extend([message, "=" * 30])
    return "\n".join(lines)


def format_enforcement_context(msgs: list[dict]) -> str:
    if not msgs:
        return ""
    return "\n\n".join(format_enforcement_banner(m) for m in msgs)


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------


def get_pretool_enforcements(session_id: str, cwd: str | Path | None = None) -> str | None:
    """Increment the counter and return formatted enforcement banners if any are due."""
    if not ENFORCEMENT_INJECTION_ENABLED:
        return None
    try:
        count = increment_and_get_count(session_id)
        due = get_due_enforcements(count, cwd)
        if not due:
            return None
        return format_enforcement_context(due)
    except Exception:
        return None


def get_posttool_enforcements(session_id: str, cwd: str | Path | None = None) -> str | None:
    if not ENFORCEMENT_INJECTION_ENABLED:
        return None
    try:
        count = int(_load_counters().get(session_id, 0))
        due = get_due_enforcements(count, cwd)
        if not due:
            return None
        return format_enforcement_context(due)
    except Exception:
        return None


def reset_session_counter(session_id: str) -> None:
    state = _load_counters()
    if session_id in state:
        del state[session_id]
        _save_counters(state)
