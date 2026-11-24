"""Token usage monitor — StatusLine metrics and threshold warnings.

Tracks context window fill percentage per session using Redis (with graceful
fallback when Redis is unavailable).  Designed to be called from the
StatusLine hook handler in hook_manager.py.

Usage::

    from hooks.observability.token_monitor import update_context_metrics

    status_line = update_context_metrics(payload)
    print(status_line)  # printed to stdout for StatusLine hook
"""

import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, Tuple

from hooks._redis import get_redis, redis_key
from hooks.common import log

FILL = "agentihooks.tokens.fill_pct"
BURN = "agentihooks.tokens.burn_rate"


def get_context_fill_pct(payload: dict) -> Optional[float]:
    """Extract fill percentage from payload context_window field.

    Returns a float 0–100, or None if the data is unavailable.
    """
    cw = payload.get("context_window")
    if not isinstance(cw, dict):
        return None
    used = cw.get("used")
    remaining = cw.get("remaining")
    if used is None or remaining is None:
        return None
    total = used + remaining
    if total <= 0:
        return None
    return used / total * 100


def persist_token_metrics(session_id: str, metrics: dict) -> None:
    """Store token metrics in Redis hash with TTL.

    Key: agenticore:tokens:{session_id}
    Fields: used, remaining, fill_pct, burn_rate, last_updated
    """
    from hooks.config import TOKEN_REDIS_TTL

    r = get_redis()
    if r is None or not session_id:
        return
    try:
        key = redis_key("tokens", session_id)
        r.hset(key, mapping={k: str(v) for k, v in metrics.items()})
        r.expire(key, TOKEN_REDIS_TTL)
    except Exception as e:
        log("token_monitor: failed to persist metrics", {"error": str(e)})


def _get_previous_used(session_id: str) -> Optional[float]:
    """Retrieve the previously stored 'used' value from Redis."""
    r = get_redis()
    if r is None or not session_id:
        return None
    try:
        key = redis_key("tokens", session_id)
        val = r.hget(key, "used")
        return float(val) if val is not None else None
    except Exception:
        return None


def should_warn_context(fill_pct: float, session_id: str) -> Tuple[bool, str]:
    """Edge-trigger warning: returns (True, level) only on first threshold crossing.

    Levels: "warning" (≥ TOKEN_WARN_PCT) or "critical" (≥ TOKEN_CRITICAL_PCT).
    Uses Redis to track whether the threshold has already been announced this session.
    Falls back to always-False when Redis is unavailable.
    """
    from hooks.config import TOKEN_CRITICAL_PCT, TOKEN_WARN_PCT

    if fill_pct >= TOKEN_CRITICAL_PCT:
        level = "critical"
    elif fill_pct >= TOKEN_WARN_PCT:
        level = "warning"
    else:
        return False, ""

    r = get_redis()
    if r is None or not session_id:
        # No Redis — emit every time so the user still sees warnings
        return True, level

    try:
        warn_key = redis_key("token_warn", session_id)
        stored = r.hget(warn_key, "level")
        if stored == level:
            # Already warned at this level
            return False, ""
        # Only escalate (warning → critical), never de-escalate
        if stored == "critical" and level == "warning":
            return False, ""
        from hooks.config import TOKEN_REDIS_TTL

        r.hset(warn_key, "level", level)
        r.expire(warn_key, TOKEN_REDIS_TTL)
        return True, level
    except Exception:
        return True, level


def update_context_metrics(payload: dict[str, Any]) -> str:
    """Compute and persist context metrics; return StatusLine string.

    Always returns a non-empty string.  On any error returns a minimal
    placeholder so the StatusLine hook never fails silently.
    """
    try:
        session_id = payload.get("session_id", "")
        cw = payload.get("context_window", {}) or {}
        used = cw.get("used", 0) or 0
        remaining = cw.get("remaining", 0) or 0
        total = used + remaining

        fill_pct = get_context_fill_pct(payload)
        if fill_pct is None:
            fill_pct = 0.0

        # Compute burn rate (delta used tokens vs previous turn)
        burn_rate: Optional[float] = None
        if session_id:
            prev = _get_previous_used(session_id)
            if prev is not None and used > 0:
                burn_rate = max(0.0, used - prev)

        # Format human-readable numbers
        def _fmt(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n // 1_000}K"
            return str(n)

        total_str = _fmt(total) if total else "?"
        used_str = _fmt(used)
        model = os.getenv("CLAUDE_MODEL", payload.get("model", "unknown"))

        parts = [f"ctx: {used_str}/{total_str} ({fill_pct:.0f}%)"]
        if burn_rate is not None:
            parts.append(f"burn: {_fmt(int(burn_rate))}/turn")
        parts.append(f"model: {model}")
        status_line = " | ".join(parts)

        # Persist metrics
        if session_id:
            metrics = {
                "used": used,
                "remaining": remaining,
                "fill_pct": round(fill_pct, 2),
                "burn_rate": int(burn_rate) if burn_rate is not None else 0,
                "last_updated": time.time(),
            }
            persist_token_metrics(session_id, metrics)
            record_tokens(session_id, fill_pct, burn_rate, time.time(), os.environ)

        return status_line

    except Exception as e:
        log("token_monitor: update_context_metrics failed", {"error": str(e)})
        return "ctx: ?"


def record_tokens(
    session_id: str, fill_pct: float, burn_rate: float | None, now_s: float, environ: Mapping[str, str]
) -> None:
    """Writes the session's last fill and burn rate, with the agent name and profile its environment names, into its
    presence seen record for the hive daemon to export, keeping every presence key and giving a record without one a
    seen time; a failed disk write never reaches the caller."""
    from hooks.observability import presence
    from hooks.observability.signals import safe_name

    path = presence.seen_dir() / f"{safe_name(session_id)}.json"
    tokens = {
        "session_id": session_id,
        "fill_pct": fill_pct,
        "burn_rate": burn_rate,
        "at_s": now_s,
        "correlation": _session_correlation(environ),
    }
    try:
        presence.write_record(path, {"seen_s": now_s, **presence.read_record(path), "tokens": tokens})
    except OSError:
        return


def _session_correlation(environ: Mapping[str, str]) -> dict[str, str]:
    """The session's agent name and validated profile under the correlation keys, which the hive daemon environment
    cannot resolve for it."""
    from hooks.observability import presence
    from hooks.observability.correlation import PREFIX

    report_path = environ.get("AGENTIHOOKS_PROFILE_REPORT")
    report = presence.read_record(Path(report_path)) if report_path else {}
    validation = report.get("validation") if report.get("state") == "validated" else None
    validated: dict[str, object] = validation if isinstance(validation, dict) else {}
    named = {"agent.name": environ.get("AGENTIHOOKS_AGENT_NAME"), "profile.resolved": validated.get("profile")}
    return {f"{PREFIX}{key}": value for key, value in named.items() if isinstance(value, str) and value}


def export_tokens(now_s: float) -> None:
    """Sends the last fill, and the burn rate when known, under its session id, agent name and profile for every seen
    record whose tokens were written within the presence fresh window."""
    from hooks.observability import otel, presence

    for path in sorted(presence.seen_dir().glob("*.json")):
        fresh = _fresh_tokens(presence.read_record(path), now_s)
        if fresh is None:
            continue
        attributes, fill_pct, burn_rate = fresh
        otel.record_gauge(FILL, fill_pct, attributes)
        if burn_rate is not None:
            otel.record_gauge(BURN, burn_rate, attributes)


def _fresh_tokens(record: Mapping[str, object], now_s: float) -> tuple[dict[str, str], float, float | None] | None:
    """The gauge attributes, fill and burn rate a seen record holds when its tokens were written within the fresh
    window."""
    from hooks.observability.presence import FRESH_S

    tokens = record.get("tokens")
    values: dict[str, object] = tokens if isinstance(tokens, dict) else {}
    keys = ("session_id", "fill_pct", "burn_rate", "at_s", "correlation")
    session_id, fill_pct, burn_rate, at_s, named = (values.get(key) for key in keys)
    if not isinstance(session_id, str) or not isinstance(fill_pct, int | float) or not isinstance(at_s, int | float):
        return None
    if now_s - at_s > FRESH_S:
        return None
    correlation: dict[str, str] = named if isinstance(named, dict) else {}
    return (
        {**correlation, "session.id": session_id},
        fill_pct,
        burn_rate if isinstance(burn_rate, int | float) else None,
    )


def record_lifecycle_context(payload: dict) -> None:
    from hooks.config import TOKEN_MONITOR_ENABLED
    from hooks.targets import is_codex

    if not TOKEN_MONITOR_ENABLED or not is_codex() or sys.platform == "win32":
        return
    if payload.get("hook_event_name") not in ("PostToolUse", "Stop"):
        return
    from scripts.codex_context import codex_context

    context = codex_context(payload.get("session_id", ""))
    if context is None or context.window <= 0:
        return
    update_context_metrics(
        {**payload, "context_window": {"used": context.used, "remaining": context.window - context.used}}
    )
