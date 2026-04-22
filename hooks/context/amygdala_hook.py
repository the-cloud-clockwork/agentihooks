"""Amygdala hook — file-based emergency signal reader.

Checks for amygdala-active.md on every UserPromptSubmit (O(1) stat).
If file exists and severity >= critical, publishes to broadcast
channel="amygdala" with persistent=True. File absence = all clear.

This is faster than waiting for brain_adapter's 30-turn refresh cycle.
The file is written by amygdala.py (brain-tools) which consumes Redis
Streams events and classifies severity deterministically.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from hooks.context.broadcast import clear_broadcasts, create_broadcast

_last_hash: str = ""
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
    global _last_hash
    from hooks._brain_http import brain_http_enabled, get

    if not brain_http_enabled():
        return False

    payload = get("/signal")
    if payload is None:
        # Transient HTTP error — leave prior state alone, do not clear.
        return True

    current_hash = payload.get("hash") or ""
    active = bool(payload.get("active"))

    if not active:
        if _last_hash:
            clear_broadcasts(channel="amygdala")
            _last_hash = ""
        return True

    if current_hash and current_hash == _last_hash:
        return True

    severity = payload.get("severity") or "critical"
    title = payload.get("title") or "AMYGDALA ALERT"
    body = payload.get("content") or ""

    clear_broadcasts(channel="amygdala")
    create_broadcast(
        message=f"[{title}]\n\n{body}",
        severity=severity,
        channel="amygdala",
        persistent=True,
        source="amygdala-hook",
    )
    _last_hash = current_hash
    return True


def check_amygdala(session_id: str) -> None:
    """Called on every UserPromptSubmit. HTTP-first, filesystem fallback."""
    global _last_hash

    if _check_via_http():
        return

    if not _SIGNAL_PATH:
        return

    path = Path(_SIGNAL_PATH)
    if not path.exists():
        if _last_hash:
            clear_broadcasts(channel="amygdala")
            _last_hash = ""
        return

    content = path.read_text()
    current_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    if current_hash == _last_hash:
        return

    fm, body = _parse_frontmatter(content)
    severity = fm.get("severity", "critical")
    title = fm.get("title", "AMYGDALA ALERT")

    clear_broadcasts(channel="amygdala")
    create_broadcast(
        message=f"[{title}]\n\n{body}",
        severity=severity,
        channel="amygdala",
        persistent=True,
        source="amygdala-hook",
    )
    _last_hash = current_hash


def clear_session_state(session_id: str) -> None:
    """Cleanup on session end."""
    global _last_hash
    _last_hash = ""
