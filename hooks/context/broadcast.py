"""Broadcast system — real-time fleet messaging for Claude Code sessions.

File-based pub/sub: operator writes messages, all active sessions receive them.
Severity levels: critical (every turn + every tool call), alert (every turn), info (once).
"""

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hooks.config import (
    BROADCAST_CRITICAL_ON_PRETOOL,
    BROADCAST_DEDUP_BY_HASH,
    BROADCAST_DELIVERY_STATE_FILE,
    BROADCAST_ENABLED,
    BROADCAST_FILE,
    BROADCAST_MAX_MESSAGES,
    BROADCAST_MAX_PER_PROMPT,
    BROADCAST_MIN_INTERVAL_SEC,
    BROADCAST_PERSISTENT_THROTTLE,
)

_SEVERITY_RANK = {"nuclear": 0, "critical": 1, "alert": 2, "warning": 3, "info": 4, "resolved": 5}


def _delivery_state_path() -> Path:
    return Path(BROADCAST_DELIVERY_STATE_FILE).expanduser()


def _load_delivery_state() -> dict:
    p = _delivery_state_path()
    if not p.exists() or p.stat().st_size == 0:
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_delivery_state(state: dict) -> None:
    p = _delivery_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(str(tmp), str(p))


def _msg_hash(msg: dict) -> str:
    # Content-only hash. Intentionally omits `id` (random UUID regenerated on
    # every clear+create cycle) and `expires_at` (changes every creation).
    # Channel + severity + message uniquely identify semantic content. Two
    # broadcasts with identical content on the same channel are the same
    # broadcast for dedup purposes, even if they carry different UUIDs.
    raw = "|".join(
        [
            str(msg.get("channel", "")),
            str(msg.get("severity", "")),
            msg.get("message", ""),
        ]
    ).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _should_skip(msg: dict, sess_state: dict, now_ts: float) -> str:
    key = msg.get("channel") or ("_msg_" + msg.get("id", ""))
    prev = sess_state.get(key)
    persistent = bool(msg.get("persistent"))
    if not prev:
        return ""
    if BROADCAST_DEDUP_BY_HASH and prev.get("hash") == _msg_hash(msg):
        if persistent and not BROADCAST_PERSISTENT_THROTTLE:
            return ""
        if now_ts - prev.get("ts", 0) < BROADCAST_MIN_INTERVAL_SEC:
            return "dedup"
    if persistent and BROADCAST_PERSISTENT_THROTTLE:
        if now_ts - prev.get("ts", 0) < BROADCAST_MIN_INTERVAL_SEC:
            return "throttle"
    return ""


def _record_delivery(sid: str, msg: dict, now_ts: float) -> None:
    state = _load_delivery_state()
    sess = state.setdefault(sid, {})
    key = msg.get("channel") or ("_msg_" + msg.get("id", ""))
    sess[key] = {"hash": _msg_hash(msg), "ts": now_ts}
    _save_delivery_state(state)


# Default TTL per severity (seconds). 0 = use default.
_DEFAULT_TTL = {"critical": 1800, "alert": 3600, "info": 14400}
_DEFAULT_PERSISTENT = {"critical": True, "alert": True, "info": False}
_VALID_SEVERITIES = frozenset({"critical", "alert", "info"})

# Channels every session is subscribed to unconditionally.
# The operator's rule: brain + amygdala are fleet-wide, no repo can drop them.
# Per-repo .agentihooks.json channels are ADDED on top of this floor.
BASE_CHANNELS = ("brain", "amygdala")


# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------


def _broadcast_path() -> Path:
    return Path(BROADCAST_FILE)


def _sessions_path() -> Path:
    return Path(BROADCAST_FILE).parent / "active-sessions.json"


# ---------------------------------------------------------------------------
# File I/O — broadcasts
# ---------------------------------------------------------------------------


def _load_broadcasts(cleanup: bool = False) -> list[dict]:
    path = _broadcast_path()
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    if cleanup:
        now = datetime.now(timezone.utc)
        before = len(data)
        data = [m for m in data if not _is_expired(m, now)]
        if len(data) != before:
            _save_broadcasts(data)

    return data


def _save_broadcasts(messages: list[dict]) -> None:
    path = _broadcast_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(messages, indent=2))
    os.replace(str(tmp), str(path))


def _is_expired(msg: dict, now: datetime | None = None) -> bool:
    expires_at = msg.get("expires_at")
    if not expires_at:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return now > exp
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# File I/O — sessions
# ---------------------------------------------------------------------------


def _save_sessions(sessions: dict) -> None:
    path = _sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, indent=2))
    os.replace(str(tmp), str(path))


def _load_sessions() -> dict:
    path = _sessions_path()
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Channel subscription resolution
# ---------------------------------------------------------------------------


def _get_session_channels(session_id: str) -> list[str]:
    """Resolve channel subscriptions for a session.

    Every session is implicitly subscribed to BASE_CHANNELS (brain, amygdala).
    Per-repo .agentihooks.json channels are added on top. A repo with no config
    file — or with no `channels` key — still receives the base floor.
    """
    base = list(BASE_CHANNELS)
    sessions = _load_sessions()
    session_info = sessions.get(session_id, {})
    cwd = session_info.get("cwd", "")
    repo_channels: list[str] = []
    if cwd:
        try:
            config_path = Path(cwd) / ".agentihooks.json"
            if config_path.exists():
                cfg = json.loads(config_path.read_text())
                ch = cfg.get("channels", [])
                if isinstance(ch, list):
                    repo_channels = ch
        except Exception:
            pass
    # Merge base + repo, deduped, preserve order (base first).
    seen: set[str] = set()
    merged: list[str] = []
    for c in list(base) + list(repo_channels):
        if c not in seen:
            seen.add(c)
            merged.append(c)
    return merged


def _message_matches_channel(msg: dict, session_channels: list[str]) -> bool:
    """Check if a message should be delivered to a session based on channels."""
    msg_channel = msg.get("channel")
    # Global messages (no channel) → always deliver
    if not msg_channel:
        return True
    # Wildcard subscription → deliver everything
    if "*" in session_channels:
        return True
    # Channel match
    return msg_channel in session_channels


# ---------------------------------------------------------------------------
# Message lifecycle
# ---------------------------------------------------------------------------


def create_broadcast(
    message: str,
    severity: str = "info",
    ttl_seconds: int = 0,
    source: str = "operator",
    persistent: bool | None = None,
    channel: str | None = None,
) -> str | None:
    if not message or not message.strip():
        return None

    if severity not in _VALID_SEVERITIES:
        severity = "alert"

    if ttl_seconds <= 0:
        ttl_seconds = _DEFAULT_TTL.get(severity, 3600)

    if persistent is None:
        persistent = _DEFAULT_PERSISTENT.get(severity, True)

    now = datetime.now(timezone.utc)
    msg_id = str(uuid.uuid4())[:12]
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")

    entry = {
        "id": msg_id,
        "message": message.strip(),
        "severity": severity,
        "persistent": persistent,
        "source": source,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "ttl_seconds": ttl_seconds,
        "expires_at": expires_at,
        "delivered_to": [],
    }
    if channel:
        entry["channel"] = channel

    msgs = _load_broadcasts()
    msgs.append(entry)

    # Enforce max messages — keep newest
    if len(msgs) > BROADCAST_MAX_MESSAGES:
        msgs = msgs[-BROADCAST_MAX_MESSAGES:]

    _save_broadcasts(msgs)
    return msg_id


def list_broadcasts() -> list[dict]:
    return _load_broadcasts()


def clear_broadcasts(message_id: str | None = None, channel: str | None = None) -> int:
    """Clear broadcasts. Returns count removed.

    If message_id: clear that specific message.
    If channel: clear all messages on that channel.
    If neither: clear everything.
    """
    msgs = _load_broadcasts()
    if message_id is None and channel is None:
        count = len(msgs)
        _save_broadcasts([])
        return count
    if channel:
        remaining = [m for m in msgs if m.get("channel") != channel]
        count = len(msgs) - len(remaining)
        _save_broadcasts(remaining)
        return count
    remaining = [m for m in msgs if m.get("id") != message_id]
    count = len(msgs) - len(remaining)
    _save_broadcasts(remaining)
    return count


# ---------------------------------------------------------------------------
# Delivery tracking
# ---------------------------------------------------------------------------


def get_pending_broadcasts(session_id: str) -> list[dict]:
    msgs = _load_broadcasts(cleanup=True)
    channels = _get_session_channels(session_id)
    pending = []
    for m in msgs:
        if _is_expired(m):
            continue
        if not _message_matches_channel(m, channels):
            continue
        if session_id in m.get("acknowledged_by", []):
            continue
        if m.get("persistent"):
            pending.append(m)
        elif session_id not in m.get("delivered_to", []):
            pending.append(m)
    return pending


def get_critical_broadcasts(session_id: str) -> list[dict]:
    msgs = _load_broadcasts(cleanup=True)
    channels = _get_session_channels(session_id)
    return [
        m
        for m in msgs
        if m.get("severity") == "critical"
        and m.get("persistent")
        and not _is_expired(m)
        and _message_matches_channel(m, channels)
        and session_id not in m.get("acknowledged_by", [])
    ]


def get_pretool_broadcasts(session_id: str) -> list[dict]:
    """Return persistent broadcasts at or above the configured severity threshold.

    Used by PreToolUse to inject alerts mid-tool-chain. Respects acknowledgment.
    """
    from hooks.config import BROADCAST_PRETOOL_MIN_SEVERITY

    min_rank = _SEVERITY_RANK.get(BROADCAST_PRETOOL_MIN_SEVERITY, 2)
    msgs = _load_broadcasts(cleanup=True)
    channels = _get_session_channels(session_id)
    return [
        m
        for m in msgs
        if _SEVERITY_RANK.get(m.get("severity", "info"), 9) <= min_rank
        and m.get("persistent")
        and not _is_expired(m)
        and _message_matches_channel(m, channels)
        and session_id not in m.get("acknowledged_by", [])
    ]


def mark_delivered(session_id: str, message_id: str) -> None:
    msgs = _load_broadcasts()
    for m in msgs:
        if m.get("id") == message_id:
            delivered = m.get("delivered_to", [])
            if session_id not in delivered:
                delivered.append(session_id)
                m["delivered_to"] = delivered
            break
    _save_broadcasts(msgs)


def acknowledge_broadcast(session_id: str, message_id: str) -> bool:
    """Mark a persistent broadcast as acknowledged for this session.

    Acknowledged messages stop re-injecting for this session but remain
    active for other sessions that haven't acknowledged.
    """
    msgs = _load_broadcasts()
    for m in msgs:
        if m.get("id") == message_id:
            acked = m.get("acknowledged_by", [])
            if session_id not in acked:
                acked.append(session_id)
                m["acknowledged_by"] = acked
            _save_broadcasts(msgs)
            return True
    return False


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------


SESSION_MAX_AGE_SECONDS = 86400  # 24h retention for crash recovery


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def encode_cwd(cwd: str) -> str:
    """Claude Code encodes cwd for ~/.claude/projects/ by replacing BOTH
    forward slashes and dots with '-'. Example:
        /home/iamroot/dev/antoncore/.claude/worktrees/foo
      → -home-iamroot-dev-antoncore--claude-worktrees-foo
    """
    return cwd.replace("/", "-").replace(".", "-")


def derive_session_title(session_id: str, cwd: str, max_len: int = 60) -> str:
    """Read the first user message from the JSONL transcript as a title.

    Fallback to the cwd basename if transcript unreadable.
    """
    try:
        transcript = Path.home() / ".claude" / "projects" / encode_cwd(cwd) / f"{session_id}.jsonl"
        if transcript.exists():
            with transcript.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "user":
                        continue
                    msg = obj.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                        content = " ".join(p for p in parts if p)
                    if not isinstance(content, str):
                        continue
                    content = content.strip().replace("\n", " ")
                    if content:
                        return content[:max_len]
    except OSError:
        pass
    return Path(cwd).name or cwd or "(unknown)"


def register_session(session_id: str, pid: int, cwd: str, model: str) -> None:
    sessions = _load_sessions()
    now = _now_iso()
    sessions[session_id] = {
        "started_at": now,
        "last_seen": now,
        "status": "alive",
        "pid": pid,
        "cwd": cwd,
        "model": model,
    }
    _save_sessions(sessions)


def deregister_session(session_id: str) -> None:
    """Hard-delete a session entry. Prefer mark_session_closed for crash-recovery."""
    sessions = _load_sessions()
    sessions.pop(session_id, None)
    _save_sessions(sessions)


def mark_session_closed(session_id: str) -> None:
    """Flip a session to status=closed on clean SessionEnd. Keeps the entry
    for the 24h retention window so `sessions reopen` can still recover it."""
    sessions = _load_sessions()
    entry = sessions.get(session_id)
    if entry is None:
        return
    entry["status"] = "closed"
    entry["last_seen"] = _now_iso()
    _save_sessions(sessions)


def heartbeat_sessions() -> dict:
    """Daemon tick: update last_seen for live PIDs, flip dead ones, prune 24h-old.

    Also calls reconcile_live_sessions (lazy import) to pick up any claude
    processes that started before this registry was deployed and thus never
    fired a SessionStart hook.
    """
    # Lazy import to avoid a circular dependency (session_registry imports
    # helpers from this module).
    try:
        from scripts.session_registry import reconcile_live_sessions

        reconcile_live_sessions()
    except Exception:
        pass

    sessions = _load_sessions()
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat().replace("+00:00", "Z")
    summary = {"alive": 0, "flipped_dead": 0, "pruned": 0, "total": 0}
    prune: list[str] = []
    changed = False

    for sid, info in list(sessions.items()):
        status = info.get("status", "alive")
        pid = info.get("pid", 0)

        ts_str = info.get("last_seen") or info.get("started_at")
        if ts_str:
            try:
                ts = _parse_iso(ts_str)
                if (now_dt - ts).total_seconds() > SESSION_MAX_AGE_SECONDS:
                    prune.append(sid)
                    continue
            except ValueError:
                pass

        if status == "alive":
            alive = False
            try:
                if pid:
                    os.kill(int(pid), 0)
                    alive = True
            except (OSError, ValueError):
                alive = False
            if alive:
                info["last_seen"] = now_iso
                summary["alive"] += 1
                changed = True
            else:
                info["status"] = "dead"
                summary["flipped_dead"] += 1
                changed = True

    for sid in prune:
        del sessions[sid]
        summary["pruned"] += 1
        changed = True

    summary["total"] = len(sessions)
    if changed:
        _save_sessions(sessions)
    return summary


def get_active_sessions(cleanup: bool = False) -> dict:
    sessions = _load_sessions()
    if cleanup:
        dead = []
        for sid, info in sessions.items():
            pid = info.get("pid")
            if pid:
                try:
                    os.kill(pid, 0)
                except (OSError, ProcessLookupError):
                    dead.append(sid)
        if dead:
            for sid in dead:
                del sessions[sid]
            _save_sessions(sessions)
    return sessions


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_broadcast_banner(msg: dict) -> str:
    severity = msg.get("severity", "alert").upper()
    source = msg.get("source", "unknown")
    message = msg.get("message", "")
    msg_id = msg.get("id", "")

    lines = [
        f"=== BROADCAST [{severity}] ===",
        f"From: {source}",
    ]
    if msg_id:
        lines.append(f"ID: {msg_id}")
    lines.extend([message, "=" * 30])
    return "\n".join(lines)


def format_critical_context(msgs: list[dict]) -> str:
    if not msgs:
        return ""
    lines = ["BROADCAST ALERTS (PreToolUse):"]
    for m in msgs:
        msg_id = m.get("id", "")
        sev = m.get("severity", "critical").upper()
        lines.append(f"  - [{sev}] (id:{msg_id}) {m['message']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hook entry points
# ---------------------------------------------------------------------------


def check_and_inject_broadcasts(session_id: str) -> None:
    if not BROADCAST_ENABLED:
        return

    try:
        from hooks.common import inject_banner
        from hooks.telemetry import emit_span

        pending = get_pending_broadcasts(session_id)
        now_ts = time.time()
        state = _load_delivery_state()
        sess_state = state.get(session_id, {})

        pending.sort(key=lambda m: _SEVERITY_RANK.get(m.get("severity", "info"), 9))

        injected = 0
        for msg in pending:
            skip_reason = _should_skip(msg, sess_state, now_ts)
            if not skip_reason and injected >= BROADCAST_MAX_PER_PROMPT:
                skip_reason = "cap"

            span_attrs = {
                "session_id": session_id,
                "message_id": msg.get("id", ""),
                "channel": msg.get("channel") or "_global",
                "severity": msg.get("severity", "info"),
                "source": msg.get("source", ""),
                "bytes": len(msg.get("message", "")),
                "persistent": bool(msg.get("persistent")),
                "skipped": bool(skip_reason),
                "skip_reason": skip_reason or "",
            }

            if skip_reason:
                emit_span("brain.delivery", span_attrs)
                continue

            banner = format_broadcast_banner(msg)
            inject_banner("BROADCAST", banner)
            emit_span("brain.delivery", span_attrs)
            _record_delivery(session_id, msg, now_ts)
            injected += 1
            if not msg.get("persistent"):
                mark_delivered(session_id, msg["id"])
    except Exception:
        pass


def get_pretool_context(session_id: str) -> str | None:
    if not BROADCAST_ENABLED or not BROADCAST_CRITICAL_ON_PRETOOL:
        return None

    try:
        msgs = get_pretool_broadcasts(session_id)
        if not msgs:
            return None
        return format_critical_context(msgs)
    except Exception:
        return None
