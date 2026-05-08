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
    BASE_CHANNELS,
    BROADCAST_CRITICAL_ON_PRETOOL,
    BROADCAST_DEDUP_BY_HASH,
    BROADCAST_DELIVERY_STATE_FILE,
    BROADCAST_ENABLED,
    BROADCAST_FILE,
    BROADCAST_MAX_BYTES_PRETOOL,
    BROADCAST_MAX_BYTES_PROMPT,
    BROADCAST_MAX_MESSAGES,
    BROADCAST_MAX_PER_PROMPT,
    BROADCAST_MIN_INTERVAL_SEC,
    BROADCAST_PERSISTENT_THROTTLE,
)

_SEVERITY_RANK = {"nuclear": 0, "critical": 1, "alert": 2, "warning": 3, "info": 4, "resolved": 5}

# Display label for the BROADCAST header. Source severity stays canonical
# (info/alert/critical) for routing; the display map prevents low-priority
# `info` content from rendering as `[ALERT]`-style framing that Claude Code
# treats as a hook-error contamination signal (issue #34713).
_SEVERITY_DISPLAY_LABEL = {
    "nuclear": "NUCLEAR",
    "critical": "CRITICAL",
    "alert": "ALERT",
    "warning": "WARNING",
    "info": "INFO",
    "resolved": "RESOLVED",
}

# Bodies that signal "no content to deliver" — emitted by the brain adapter
# when the upstream feed is empty. Suppressed before formatting so the model
# does not see noise framed as an alert.
_EMPTY_BODY_SENTINELS = frozenset(
    {
        "no active signals.",
        "no inject blocks.",
        "no data.",
        "none.",
        "",
    }
)


def _body_is_empty(msg: dict) -> bool:
    """Return True for messages whose body conveys no signal."""
    body = (msg.get("message") or "").strip()
    if not body:
        return True
    # Strip leading "[Title]\n" prefix if present so titled empty bodies
    # like "[Active Signals]\nNo active signals." are also caught.
    if body.startswith("[") and "]\n" in body:
        body = body.split("]\n", 1)[1].strip()
    return body.lower() in _EMPTY_BODY_SENTINELS


def _truncate_body(body: str, max_bytes: int) -> str:
    """Truncate utf-8 body to max_bytes with a marker, preserving char boundaries.

    max_bytes <= 0 → no cap; return body unchanged. Lets operators run with
    truncation off by default and opt in via env var when they need
    envelope headroom under Claude Code's 10K hook output limit.
    """
    if max_bytes <= 0:
        return body
    encoded = body.encode("utf-8")
    if len(encoded) <= max_bytes:
        return body
    marker = "\n[...truncated]"
    keep = max(0, max_bytes - len(marker.encode("utf-8")))
    truncated = encoded[:keep].decode("utf-8", errors="ignore")
    return truncated + marker


def _display_label(severity: str) -> str:
    return _SEVERITY_DISPLAY_LABEL.get(severity.lower(), severity.upper())


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
    # Dedup per-message, not per-channel. A channel like "brain" carries many
    # distinct entries (hot-arcs, operator-intent, tick-diff, signals…); keying
    # on channel collapses them all into one bucket and throttles 4-5 entries
    # as if they were duplicates of a single broadcast — agents end up seeing
    # only the first 1-2 to land. Keying on message id keeps each entry on its
    # own throttle clock.
    key = msg.get("id") or msg.get("channel") or "_global"
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
    # Match _should_skip: per-message id, not per-channel.
    key = msg.get("id") or msg.get("channel") or "_global"
    sess[key] = {"hash": _msg_hash(msg), "ts": now_ts}
    _save_delivery_state(state)


# Default TTL per severity (seconds). 0 = use default.
_DEFAULT_TTL = {"nuclear": 1800, "critical": 1800, "alert": 3600, "warning": 3600, "info": 14400, "resolved": 1800}
_DEFAULT_PERSISTENT = {
    "nuclear": True,
    "critical": True,
    "alert": True,
    "warning": True,
    "info": False,
    "resolved": False,
}
_VALID_SEVERITIES = frozenset({"nuclear", "critical", "alert", "warning", "info", "resolved"})

# BASE_CHANNELS is imported from hooks.config (env-driven via
# AGENTIHOOKS_BASE_CHANNELS). Layered: profile env → repo settings.json → repo
# settings.local.json → container ENV. Empty / unset → session only receives
# global broadcasts (no `channel` field). Re-exported here so callers can
# `from hooks.context.broadcast import BASE_CHANNELS` without changes.


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

    Subscriptions come from the `AGENTIHOOKS_BASE_CHANNELS` env var (parsed
    once at `hooks.config` import time). Layered via Claude Code settings.json
    `env` block: profile default → repo override → repo-local override →
    container launch env.
    """
    return list(BASE_CHANNELS)


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
    # Stable content hash decouples dedup from the random uuid above so the
    # brain adapter (which republishes the same content with fresh ids each
    # tick) does not generate false-novelty injections.
    entry["content_hash"] = _msg_hash(entry)

    msgs = _load_broadcasts()
    msgs.append(entry)

    # Enforce max messages — keep newest
    if len(msgs) > BROADCAST_MAX_MESSAGES:
        msgs = msgs[-BROADCAST_MAX_MESSAGES:]

    _save_broadcasts(msgs)
    return msg_id


def find_broadcast_by_content_hash(content_hash: str, channel: str | None = None) -> dict | None:
    """Return the most recent broadcast matching content_hash + channel, else None."""
    if not content_hash:
        return None
    msgs = _load_broadcasts()
    matches = [
        m for m in msgs if m.get("content_hash") == content_hash and (channel is None or m.get("channel") == channel)
    ]
    if not matches:
        return None
    matches.sort(key=lambda m: m.get("created_at", ""))
    return matches[-1]


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
    """Return the session's display name.

    Priority:
      1. Most recent `custom-title` event (set by Claude Code /rename or --name flag)
      2. Most recent `agent-name` event
      3. First user message text
      4. cwd basename (fallback when transcript unreadable)
    """
    try:
        transcript = Path.home() / ".claude" / "projects" / encode_cwd(cwd) / f"{session_id}.jsonl"
        if transcript.exists():
            custom_title: str | None = None
            agent_name: str | None = None
            first_user_msg: str | None = None
            with transcript.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_type = obj.get("type")
                    if event_type == "custom-title":
                        t = obj.get("customTitle", "")
                        if t:
                            custom_title = t
                    elif event_type == "agent-name":
                        n = obj.get("agentName", "")
                        if n:
                            agent_name = n
                    elif event_type == "user" and first_user_msg is None:
                        msg = obj.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                            content = " ".join(p for p in parts if p)
                        if isinstance(content, str):
                            content = content.strip().replace("\n", " ")
                            if content:
                                first_user_msg = content
            if custom_title:
                return custom_title[:max_len]
            if agent_name:
                return agent_name[:max_len]
            if first_user_msg:
                return first_user_msg[:max_len]
    except OSError:
        pass
    return Path(cwd).name or cwd or "(unknown)"


def register_session(session_id: str, pid: int, cwd: str, model: str) -> None:
    sessions = _load_sessions()
    now = _now_iso()
    # A single Claude Code PID only hosts ONE active session at a time.
    # When a new session_id registers from the same pid, supersede any
    # previously-alive entries for that pid (they're from an earlier
    # session lifecycle — /resume or /clear).
    if pid:
        for existing_sid, existing_info in sessions.items():
            if existing_sid == session_id:
                continue
            if existing_info.get("pid") == pid and existing_info.get("status") == "alive":
                existing_info["status"] = "superseded"
                existing_info["superseded_at"] = now
                existing_info["superseded_by"] = session_id
    # Preserve started_at across re-registrations (SessionStart can fire
    # multiple times per session — resume, reconnect — and we want the
    # age to reflect the true session start, not the last event).
    existing = sessions.get(session_id)
    started_at = existing.get("started_at", now) if existing else now
    sessions[session_id] = {
        "started_at": started_at,
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


def get_active_sessions(cleanup: bool = False, include_all: bool = False) -> dict:
    """Return session entries from the registry.

    By default returns only entries with status="alive" — this matches the
    semantic of "active" (one per live PID, after the supersede fix).
    Pass include_all=True to get the full registry including superseded,
    closed, and dead entries.

    When cleanup=True, entries whose PID is gone are marked "dead" (not
    deleted — preserved for the 24h retention window used by
    `sessions list`).
    """
    sessions = _load_sessions()
    if cleanup:
        changed = False
        for sid, info in sessions.items():
            pid = info.get("pid")
            if not pid or info.get("status") in ("dead", "closed", "superseded"):
                continue
            try:
                os.kill(pid, 0)
            except (OSError, ProcessLookupError):
                info["status"] = "dead"
                changed = True
        if changed:
            _save_sessions(sessions)
    if include_all:
        return sessions
    return {sid: info for sid, info in sessions.items() if info.get("status") == "alive"}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_broadcast_banner(msg: dict) -> str:
    severity_raw = msg.get("severity", "alert")
    severity = _display_label(severity_raw)
    source = msg.get("source", "unknown")
    message = _truncate_body(msg.get("message", ""), BROADCAST_MAX_BYTES_PROMPT)
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
    # Drop empty-body messages — `[ALERT] No active signals.` is a #34713
    # contamination vector and conveys zero signal.
    msgs = [m for m in msgs if not _body_is_empty(m)]
    if not msgs:
        return ""
    # Dedup within one formatting pass: when multiple messages share a
    # content_hash, keep only the most recent (highest created_at).
    seen: dict[str, dict] = {}
    for m in msgs:
        h = m.get("content_hash") or _msg_hash(m)
        prev = seen.get(h)
        if prev is None or (m.get("created_at", "") > prev.get("created_at", "")):
            seen[h] = m
    deduped = list(seen.values())
    deduped.sort(key=lambda m: _SEVERITY_RANK.get(m.get("severity", "info"), 9))

    header = "BROADCAST ALERTS (PreToolUse):"
    lines = [header]
    if BROADCAST_MAX_BYTES_PRETOOL <= 0:
        for m in deduped:
            msg_id = m.get("id", "")
            sev = _display_label(m.get("severity", "alert"))
            lines.append(f"  - [{sev}] (id:{msg_id}) {m.get('message', '')}")
    else:
        budget = max(0, BROADCAST_MAX_BYTES_PRETOOL - len(header.encode("utf-8")) - 1)
        for m in deduped:
            msg_id = m.get("id", "")
            sev = _display_label(m.get("severity", "alert"))
            body = _truncate_body(m.get("message", ""), max(0, budget // max(1, len(deduped))))
            line = f"  - [{sev}] (id:{msg_id}) {body}"
            line_bytes = len(line.encode("utf-8")) + 1
            if line_bytes > budget:
                break
            lines.append(line)
            budget -= line_bytes
    if len(lines) == 1:
        return ""
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
            if not skip_reason and _body_is_empty(msg):
                skip_reason = "empty"
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
