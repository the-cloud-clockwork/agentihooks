"""Rule refresh — one-shot re-injection of profile rules into running sessions.

When an operator runs `agentihooks refresh-rules`, a marker file is written
containing the current rule content and a snapshot of session IDs that were
alive at the time. On each session's next UserPromptSubmit, the hook checks
the marker; if that session is in the pending list, it receives the rule
payload as additionalContext and is removed from the list.

Properties:
- Running sessions get the one-shot on their next prompt
- Sessions started AFTER the refresh never see the marker (they got fresh
  rules at SessionStart, so they don't need it)
- Each session consumes the marker at most once
- Markers auto-GC after MAX_AGE_SECONDS
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from hooks.common import inject_context, log

MAX_AGE_SECONDS = 24 * 3600  # 24h — markers older than this are GCed


def _refresh_dir() -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "force_refresh"


def _marker_path(profile: str) -> Path:
    return _refresh_dir() / f"rules-{profile}.json"


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_marker(profile: str) -> dict | None:
    path = _marker_path(profile)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log("rules_refresh: marker read failed", {"profile": profile, "error": str(e)})
        return None


def _save_marker(profile: str, marker: dict) -> None:
    path = _marker_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(marker, indent=2))


def _delete_marker(profile: str) -> None:
    try:
        _marker_path(profile).unlink(missing_ok=True)
    except OSError:
        pass


def _is_expired(marker: dict) -> bool:
    try:
        ts = datetime.strptime(marker["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        age = (datetime.now(UTC) - ts).total_seconds()
        return age > MAX_AGE_SECONDS
    except (KeyError, ValueError):
        return True  # malformed marker — treat as expired


def _collect_pending_sessions() -> list[str]:
    """Snapshot of currently-alive session IDs from the broadcast registry.

    Only includes sessions marked 'alive' — dead or pruned sessions are skipped.
    """
    try:
        from hooks.context.broadcast import _load_sessions

        sessions = _load_sessions()
    except Exception as e:
        log("rules_refresh: session snapshot failed", {"error": str(e)})
        return []

    pending = []
    for sid, info in sessions.items():
        if info.get("status") == "alive":
            pending.append(sid)
    return pending


def write_refresh_marker(profile: str, payload: str) -> dict:
    """Write a one-shot refresh marker. Called by `agentihooks refresh-rules` CLI.

    Returns a summary dict with the written marker info.
    """
    pending = _collect_pending_sessions()
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    marker = {
        "ts": _iso_now(),
        "profile": profile,
        "content_hash": content_hash,
        "payload": payload,
        "pending": pending,
    }
    _save_marker(profile, marker)
    log(
        "rules_refresh: marker written",
        {
            "profile": profile,
            "content_hash": content_hash,
            "pending_count": len(pending),
        },
    )
    return {
        "profile": profile,
        "content_hash": content_hash,
        "pending_count": len(pending),
        "pending": pending,
        "marker_path": str(_marker_path(profile)),
    }


def maybe_inject(session_id: str) -> None:
    """PromptSubmit handler: inject rule payload if this session is pending.

    - If no marker exists → no-op
    - If marker is expired → delete it, no-op
    - If session_id not in pending → no-op
    - Otherwise: inject, remove session from pending, save (or delete if last one)
    """
    if not session_id:
        return
    refresh_dir = _refresh_dir()
    if not refresh_dir.exists():
        return

    for marker_file in refresh_dir.glob("rules-*.json"):
        profile = marker_file.stem.removeprefix("rules-")
        marker = _load_marker(profile)
        if not marker:
            continue

        if _is_expired(marker):
            log("rules_refresh: expired marker GCed", {"profile": profile})
            _delete_marker(profile)
            continue

        pending = marker.get("pending", [])
        if session_id not in pending:
            continue

        # Deliver the one-shot
        payload = marker.get("payload", "")
        if payload:
            inject_context(payload)
            log(
                "rules_refresh: delivered",
                {"profile": profile, "session_id": session_id, "content_hash": marker.get("content_hash", "")},
            )

        # Consume: remove this session from pending
        pending = [s for s in pending if s != session_id]
        if not pending:
            _delete_marker(profile)
            log("rules_refresh: marker drained", {"profile": profile})
        else:
            marker["pending"] = pending
            _save_marker(profile, marker)


def gc_all_expired() -> int:
    """Sweep and delete all expired markers. Returns count deleted."""
    refresh_dir = _refresh_dir()
    if not refresh_dir.exists():
        return 0
    deleted = 0
    for marker_file in refresh_dir.glob("rules-*.json"):
        profile = marker_file.stem.removeprefix("rules-")
        marker = _load_marker(profile)
        if not marker or _is_expired(marker):
            _delete_marker(profile)
            deleted += 1
    return deleted


def collect_profile_rules(
    profile_rules_dir: Path,
    claude_md_path: Path | None = None,
    claude_local_md_path: Path | None = None,
) -> str:
    """Read a profile's rules directory + CLAUDE.md + CLAUDE.local.md into a single injection payload.

    Order of inclusion (highest precedence last so it can override):
      1. CLAUDE.md (global profile)
      2. rules/*.md (profile rules, sorted)
      3. CLAUDE.local.md (user-level local override)

    Returns a formatted string suitable for inject_context.
    """
    parts: list[str] = []
    parts.append("=== PROFILE RULES REFRESH ===")
    parts.append(f"Refreshed at: {_iso_now()}")
    parts.append("Rule files on disk have changed. Below is the current state.")
    parts.append("These rules supersede the versions loaded at SessionStart.")
    parts.append("")

    if claude_md_path and claude_md_path.exists():
        parts.append(f"--- {claude_md_path.name} ---")
        parts.append(claude_md_path.read_text())
        parts.append("")

    if profile_rules_dir.exists():
        rule_files = sorted(profile_rules_dir.glob("*.md"))
        for rf in rule_files:
            parts.append(f"--- rules/{rf.name} ---")
            parts.append(rf.read_text())
            parts.append("")

    if claude_local_md_path and claude_local_md_path.exists():
        parts.append(f"--- {claude_local_md_path.name} (local override) ---")
        parts.append(claude_local_md_path.read_text())
        parts.append("")

    parts.append("=== END PROFILE RULES REFRESH ===")
    return "\n".join(parts)
