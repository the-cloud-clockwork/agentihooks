"""Prompts a session scheduled for itself with a cron or wakeup tool, which later fire as prompts the operator never typed.

The UserPromptSubmit payload carries no origin field, so the prompt text recorded when the tool ran is the signal.
"""

import json
import os
import re
from pathlib import Path

SCHEDULERS = ("CronCreate", "ScheduleWakeup")
KEPT = 50
AUTOMATED = (
    "Automated prompt: a session cron, scheduled task, nudge or swarm delivery fired this, not the operator. "
    "Act on it and write nothing to the operator."
)


def _norm(text: object) -> str:
    return " ".join(str(text or "").split())


def _path(session_id: str) -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "scheduled_prompts" / (re.sub(r"[^A-Za-z0-9_.@-]", "_", session_id) + ".json")


def _load(session_id: str) -> list[str]:
    try:
        prompts = json.loads(_path(session_id).read_text())
    except (OSError, ValueError):
        return []
    return [p for p in prompts if isinstance(p, str)] if isinstance(prompts, list) else []


def record(payload: dict) -> bool:
    """Remember the prompt a cron or wakeup tool call scheduled in this session."""
    session_id = payload.get("session_id") or ""
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") not in SCHEDULERS or not session_id or not isinstance(tool_input, dict):
        return False
    prompt = _norm(tool_input.get("prompt"))
    if not prompt:
        return False
    prompts = [p for p in _load(session_id) if p != prompt] + [prompt]
    path = _path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(prompts[-KEPT:]))
    os.replace(tmp, path)
    return True


def fired(session_id: str, prompt: object) -> bool:
    """True when the prompt is one this session scheduled for itself."""
    text = _norm(prompt)
    return bool(session_id and text) and text in _load(session_id)
