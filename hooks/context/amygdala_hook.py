"""Amygdala hook — file-based emergency signal reader.

Checks for amygdala-active.md on every UserPromptSubmit (O(1) stat).
If file exists and severity >= critical, publishes to broadcast
channel="amygdala" with persistent=True. File absence = all clear.

This is faster than waiting for brain_adapter's 30-turn refresh cycle.
The file is written by amygdala.py (brain-tools) which consumes Redis
Streams events and classifies severity deterministically.
"""

from __future__ import annotations

import os
from pathlib import Path

from hooks.context.broadcast import reconcile_channel_broadcasts

_SIGNAL_PATH: str = os.getenv("AMYGDALA_SIGNAL_PATH", "")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal frontmatter parser (no PyYAML dep)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    return fm, parts[2].strip()


def _check_via_http() -> bool:
    """Poll kb-router /signal. Returns True when handled (even if no signal)."""
    from hooks._brain_http import brain_http_enabled, get

    if not brain_http_enabled():
        return False

    payload = get("/signal")
    if payload is None:
        # Transient HTTP error — leave prior state alone, do not clear.
        return True

    active = bool(payload.get("active"))

    if not active:
        reconcile_channel_broadcasts("amygdala", [])
        return True

    severity = payload.get("severity") or "critical"
    title = payload.get("title") or "AMYGDALA ALERT"
    body = payload.get("content") or ""

    reconcile_channel_broadcasts(
        "amygdala",
        [
            {
                "message": f"[{title}]\n\n{body}",
                "severity": severity,
                "persistent": True,
                "source": "amygdala-hook",
            }
        ],
    )
    return True


def check_amygdala(session_id: str) -> None:
    """Called on every UserPromptSubmit. HTTP-first, filesystem fallback."""
    if _check_via_http():
        return

    if not _SIGNAL_PATH:
        return

    path = Path(_SIGNAL_PATH)
    if not path.exists():
        reconcile_channel_broadcasts("amygdala", [])
        return

    content = path.read_text()

    fm, body = _parse_frontmatter(content)
    severity = fm.get("severity", "critical")
    title = fm.get("title", "AMYGDALA ALERT")

    reconcile_channel_broadcasts(
        "amygdala",
        [
            {
                "message": f"[{title}]\n\n{body}",
                "severity": severity,
                "persistent": True,
                "source": "amygdala-hook",
            }
        ],
    )


def clear_session_state(session_id: str) -> None:
    return None
