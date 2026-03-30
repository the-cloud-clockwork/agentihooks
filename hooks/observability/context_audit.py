"""Context audit — tracks per-tool token consumption across a session.

Records cumulative byte sizes of tool outputs, providing visibility into
what's consuming the context window.  On session Stop, emits a compact
report of the top consumers when fill_pct exceeds a threshold.

Redis key:
    agenticore:context_audit:{session_id}  — Hash of tool_name → cumulative_bytes

Falls back to an in-process dict when Redis is unavailable.
"""

from hooks._redis import get_redis, redis_key
from hooks.common import log

# In-process fallback: {session_id: {tool_name: cumulative_bytes}}
_memory_audit: dict[str, dict[str, int]] = {}


def record_tool_usage(session_id: str, tool_name: str, output_size_bytes: int) -> None:
    """Increment cumulative byte count for a tool in this session."""
    if not session_id or not tool_name or output_size_bytes <= 0:
        return

    r = get_redis()
    if r:
        try:
            key = redis_key("context_audit", session_id)
            r.hincrby(key, tool_name, output_size_bytes)
            from hooks.config import TOKEN_REDIS_TTL
            r.expire(key, TOKEN_REDIS_TTL)
            return
        except Exception:
            pass

    # Fallback to in-memory
    if session_id not in _memory_audit:
        _memory_audit[session_id] = {}
    bucket = _memory_audit[session_id]
    bucket[tool_name] = bucket.get(tool_name, 0) + output_size_bytes


def get_audit_summary(session_id: str) -> dict[str, int]:
    """Return {tool_name: cumulative_bytes} for the session."""
    if not session_id:
        return {}

    r = get_redis()
    if r:
        try:
            key = redis_key("context_audit", session_id)
            raw = r.hgetall(key)
            return {k.decode() if isinstance(k, bytes) else k:
                    int(v) for k, v in raw.items()}
        except Exception:
            pass

    return dict(_memory_audit.get(session_id, {}))


def _fmt_bytes(n: int) -> str:
    """Format byte count as compact human string."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def format_audit_report(summary: dict[str, int], fill_pct: float) -> str:
    """Format a compact audit report of top context consumers.

    Returns empty string if summary is empty.
    """
    if not summary:
        return ""

    sorted_tools = sorted(summary.items(), key=lambda x: x[1], reverse=True)[:5]
    total = sum(summary.values())

    lines = [f"Context audit (fill: {fill_pct:.0f}%, total tool output: {_fmt_bytes(total)}):"]
    for tool, nbytes in sorted_tools:
        pct = nbytes / total * 100 if total > 0 else 0
        lines.append(f"  {tool}: {_fmt_bytes(nbytes)} ({pct:.0f}%)")

    return "\n".join(lines)


def clear_session_audit(session_id: str) -> None:
    """Remove audit data for a session."""
    if not session_id:
        return

    r = get_redis()
    if r:
        try:
            r.delete(redis_key("context_audit", session_id))
        except Exception:
            pass

    _memory_audit.pop(session_id, None)
