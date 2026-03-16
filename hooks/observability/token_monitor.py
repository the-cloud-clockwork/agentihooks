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
import time
from typing import Optional, Tuple

from hooks._redis import get_redis, redis_key
from hooks.common import log


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


def update_context_metrics(payload: dict) -> str:
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

        return status_line

    except Exception as e:
        log("token_monitor: update_context_metrics failed", {"error": str(e)})
        return "ctx: ?"
