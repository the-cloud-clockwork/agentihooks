"""Autonomy enforcer — blocks premature Stop events when bypass mode is active.

The CLAUDE.md autonomy directive ("never suggest stopping, defer non-critical
blockers, keep moving") is model guidance and not always honored — Claude
sometimes stalls after a tool failure or returns control mid-task. This hook
detects that pattern at Stop time and emits the Claude Code Stop-hook block
directive (`{"decision": "block", "reason": "..."}`) to force continuation.

Trigger conditions (ALL must hold):
    1. AUTONOMY_ENFORCER_ENABLED is true
    2. Bypass mode is active for the session (operator opted into autonomy)
    3. payload.stop_hook_active is False (prevents infinite block loops —
       Claude Code sets this true when the previous Stop was already blocked)
    4. Block count for session < AUTONOMY_BLOCK_MAX (hard cap)
    5. Transcript shows a recent tool error (is_error=True within last
       AUTONOMY_LOOKBACK transcript entries) — i.e. the stop correlates with
       a tool failure, which is the exact pattern operators report
    6. Last user message does not contain an explicit stop signal
       ("stop", "wait", "pause", "cancel", "hold on", "halt")

Block budget tracked per session in ~/.agentihooks/autonomy_blocks/<sid>.count.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from hooks.common import log
from hooks.config import AGENTIHOOKS_HOME

# ---------------------------------------------------------------------------
# Block count tracking (file-based — survives across hook subprocesses)
# ---------------------------------------------------------------------------

_COUNT_DIR = AGENTIHOOKS_HOME / "autonomy_blocks"

# Stop signals — if user typed any of these, do NOT block the stop
_STOP_PATTERNS = re.compile(
    r"\b(stop|wait|pause|cancel|hold on|halt|abort|enough|never mind)\b",
    re.IGNORECASE,
)


def _count_path(session_id: str) -> Path:
    return _COUNT_DIR / f"{session_id}.count"


def _read_count(session_id: str) -> int:
    if not session_id:
        return 0
    path = _count_path(session_id)
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip() or "0")
    except (ValueError, OSError):
        return 0


def _bump_count(session_id: str) -> int:
    if not session_id:
        return 0
    try:
        _COUNT_DIR.mkdir(parents=True, exist_ok=True)
        n = _read_count(session_id) + 1
        _count_path(session_id).write_text(str(n))
        return n
    except OSError:
        return 0


def clear_count(session_id: str) -> None:
    if not session_id:
        return
    try:
        _count_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Transcript scanning
# ---------------------------------------------------------------------------


def _last_n_transcript_entries(transcript_path: str, n: int) -> list[dict]:
    """Return the last n parsed JSONL entries from the transcript."""
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists():
        return []
    try:
        with open(p, "r") as f:
            lines = f.readlines()
    except OSError:
        return []

    out: list[dict] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _has_recent_tool_error(entries: list[dict]) -> bool:
    """Return True if any tool_result block in entries has is_error=True."""
    for entry in entries:
        if entry.get("type") != "user":
            continue
        message = entry.get("message", {}) or {}
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if block.get("is_error"):
                    return True
    return False


def _last_user_text(entries: list[dict]) -> str:
    """Return the most recent user message text (joined), empty if none."""
    for entry in reversed(entries):
        if entry.get("type") != "user":
            continue
        message = entry.get("message", {}) or {}
        content = message.get("content") if isinstance(message, dict) else None

        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                # Skip tool_result blocks — they're tool output, not user input
                if block.get("type") == "tool_result":
                    continue
                if block.get("type") == "text":
                    t = block.get("text", "")
                    if t:
                        texts.append(t)
            if texts:
                return "\n".join(texts)
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_stop_block(payload: dict) -> dict[str, Any] | None:
    """Decide whether to block this Stop event.

    Returns the Claude Code decision dict ({"decision": "block", "reason": ...})
    if the stop should be blocked, or None to let it proceed normally.
    Logs every decision for observability.
    """
    from hooks.config import (
        AUTONOMY_BLOCK_MAX,
        AUTONOMY_ENFORCER_ENABLED,
        AUTONOMY_LOOKBACK,
    )
    from hooks.context.controls_toggle import is_controls_disabled

    if not AUTONOMY_ENFORCER_ENABLED:
        return None

    session_id = payload.get("session_id", "")
    if not session_id:
        return None

    # Anti-loop guard — Claude Code sets this when the previous Stop was already blocked
    if payload.get("stop_hook_active"):
        log("autonomy_enforcer: stop_hook_active=true, allowing stop", {"session_id": session_id})
        return None

    if not is_controls_disabled():
        return None  # Bypass mode not active — operator did not opt into autonomy

    block_count = _read_count(session_id)
    if block_count >= AUTONOMY_BLOCK_MAX:
        log(
            "autonomy_enforcer: block budget exhausted, allowing stop",
            {"session_id": session_id, "block_count": block_count, "max": AUTONOMY_BLOCK_MAX},
        )
        return None

    transcript_path = payload.get("transcript_path", "")
    entries = _last_n_transcript_entries(transcript_path, AUTONOMY_LOOKBACK)

    if not _has_recent_tool_error(entries):
        return None  # No recent failure — no autonomy violation to enforce

    user_text = _last_user_text(entries)
    if user_text and _STOP_PATTERNS.search(user_text):
        log(
            "autonomy_enforcer: explicit stop signal in last user message, allowing stop",
            {"session_id": session_id},
        )
        return None

    new_count = _bump_count(session_id)
    reason = (
        "AUTONOMY ENFORCER: bypass mode is active and the last tool call returned "
        "an error. Per CLAUDE.md autonomy directive, you must NOT end the turn on a "
        "tool failure. Defer the blocker to /tmp/full-clearance-defer.md "
        "(format: `[ISO timestamp] [SEVERITY] description | context | next-step`) "
        "and continue with the next executable step. If the failure is a hard block "
        "(secrets in plaintext, main/prod operation, sudo dependency), state the "
        f"block clearly and ask the operator. Block #{new_count}/"
        f"{AUTONOMY_BLOCK_MAX} for this session."
    )

    log(
        "autonomy_enforcer: blocking stop after tool error",
        {
            "session_id": session_id,
            "block_count": new_count,
            "max": AUTONOMY_BLOCK_MAX,
            "ts": time.time(),
        },
    )

    return {"decision": "block", "reason": reason}
