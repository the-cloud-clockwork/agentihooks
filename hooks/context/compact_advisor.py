"""Smart compact suggestion engine — replaces generic /compact warnings.

Uses context audit data to produce actionable compact focus phrases
that tell users exactly what to compact and why.
"""

from typing import Optional


def _fmt_bytes(n: int) -> str:
    """Format byte count as compact human string."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def suggest_compact_focus(audit_summary: dict[str, int]) -> str:
    """Build a compact focus phrase from audit data.

    Args:
        audit_summary: {tool_name: cumulative_bytes} from context_audit

    Returns:
        Focus phrase string, or empty string if no data.
    """
    if not audit_summary:
        return ""

    sorted_tools = sorted(audit_summary.items(), key=lambda x: x[1], reverse=True)[:3]
    parts = [f"{tool} ({_fmt_bytes(nbytes)})" for tool, nbytes in sorted_tools]
    return "top consumers: " + ", ".join(parts)


def format_suggestion(fill_pct: float, level: str, audit_summary: Optional[dict[str, int]] = None) -> str:
    """Format a smart compact suggestion with actionable detail.

    Args:
        fill_pct: Current context fill percentage
        level: "warning" or "critical"
        audit_summary: Optional audit data for smart suggestions

    Returns:
        Formatted warning string (without ANSI — caller applies color).
    """
    focus = suggest_compact_focus(audit_summary) if audit_summary else ""

    if level == "critical":
        base = f"CONTEXT {fill_pct:.0f}% — /compact now or start new session"
        if focus:
            return f"{base} — {focus}"
        return base
    else:
        base = f"CONTEXT {fill_pct:.0f}% — consider /compact soon"
        if focus:
            return f"{base} — {focus}"
        return base
