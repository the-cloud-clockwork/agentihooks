"""Prompts a session scheduled for itself with a cron or wakeup tool, which later fire as prompts the operator never typed.

The UserPromptSubmit payload carries no origin field, so the prompt text recorded when the tool ran is the signal.
Each prompt is kept under the session and under the agent process, so a cleared session still finds its crons.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, cast

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


def _agent() -> str:
    from hooks.context.account_sessions import agent_pid

    return f"agent-{agent_pid()}"


def _owners(session_id: str) -> tuple[str, str]:
    return session_id, _agent()


def _load(owner: str) -> list[str]:
    try:
        prompts: object = json.loads(_path(owner).read_text())
    except (OSError, ValueError):
        return []
    return [p for p in cast(list[object], prompts) if isinstance(p, str)] if isinstance(prompts, list) else []


def record(payload: dict[str, Any]) -> bool:
    """Remember the prompt a cron or wakeup tool call scheduled in this session."""
    session_id = str(payload.get("session_id") or "")
    tool_input: object = payload.get("tool_input")
    if payload.get("tool_name") not in SCHEDULERS or not session_id or not isinstance(tool_input, dict):
        return False
    prompt = _norm(cast(dict[str, object], tool_input).get("prompt"))
    if not prompt:
        return False
    return all([_store(owner, prompt) for owner in _owners(session_id)])


def _store(owner: str, prompt: str) -> bool:
    prompts = [p for p in _load(owner) if p != prompt] + [prompt]
    path = _path(owner)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(prompts[-KEPT:]))
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def fired(session_id: str, prompt: object) -> bool:
    """True when the prompt is one this session or its agent process scheduled for itself."""
    text = _norm(prompt)
    return bool(session_id and text) and any(text in _load(owner) for owner in _owners(session_id))


def forget(payload: dict[str, Any]) -> None:
    """Drop the session's record at session end, and the agent's record unless a clear keeps the process."""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    owners = _owners(session_id)
    for owner in owners if payload.get("reason") != "clear" else owners[:1]:
        _drop(owner)


def _drop(owner: str) -> bool:
    try:
        _path(owner).unlink(missing_ok=True)
    except OSError:
        return False
    return True
