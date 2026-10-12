"""The master hotfix window: the operator's typed allow-master-coding lets the master seat code for thirty minutes.

Only a prompt the operator typed in the master pane opens or closes it; agent text, tool output and relays never do.
"""

import json
import os
import re
import time
from pathlib import Path

WINDOW_SEC = 1800
OPEN = re.compile(r"(?<![\w-])allow-master-coding(?![\w-])")
CLOSE = re.compile(r"(?<![\w-])disallow-master-coding(?![\w-])")
PERMISSION_TOOLS = ("AskUserQuestion", "ExitPlanMode")
PROTECTION = re.compile(r"\bgh\s+(?:ruleset\b|api\b[^\n;&|]*(?:/rulesets\b|/branches/[^\s/]+/protection\b))")
NOTICES = {
    "open": "Master hotfix window open for thirty minutes: the master may create a worktree with wt.sh new, edit "
    "inside it, commit, push its branch and merge its pull request into dev on green. Main, rulesets, branch "
    "protection, condition folders, credential files and secrets stay closed. disallow-master-coding ends it.",
    "closed": "Master hotfix window closed: the master no longer edits code.",
}
PROTECTION_REFUSAL = (
    "BLOCKED: the master never changes rulesets or branch protection, inside the hotfix window or out of it."
)


def is_master() -> bool:
    return bool(os.environ.get("AGENTIHOOKS_SWARM")) and os.environ.get("AGENTIHOOKS_SWARM_TASK") == "master"


def path(session_id: str) -> Path:
    home = Path(os.environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")
    return home / "master_coding" / (re.sub(r"[^A-Za-z0-9_.@-]", "_", session_id) + ".json")


def observe(session_id: str | None, prompt: str | None, typed: bool, now: float | None = None) -> str:
    """Open or close the window on the operator's typed phrase in the master pane; returns open, closed or ''."""
    if not (session_id and typed and is_master()):
        return ""
    if CLOSE.search(str(prompt)):
        path(session_id).unlink(missing_ok=True)
        return "closed"
    if not OPEN.search(str(prompt)):
        return ""
    path(session_id).parent.mkdir(parents=True, exist_ok=True)
    path(session_id).write_text(json.dumps({"at": time.time() if now is None else now}))
    return "open"


def is_open(session_id: str, now: float | None = None) -> bool:
    if not (session_id and is_master()):
        return False
    try:
        opened = json.loads(path(session_id).read_text())["at"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (time.time() if now is None else now) - opened < WINDOW_SEC


def asks_operator(tool_name: str, session_id: str | None) -> bool:
    """Whether a permission prompt for this tool may reach the operator present in the master pane."""
    from hooks.context.operator_mode import _heard

    return tool_name in PERMISSION_TOOLS and is_master() and _heard(session_id, None, None)


def protection_refusal(payload: dict) -> str:
    command = str((payload.get("tool_input") or {}).get("command"))
    return PROTECTION_REFUSAL if is_master() and PROTECTION.search(command) else ""
