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


def is_master(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get("AGENTIHOOKS_SWARM")) and env.get("AGENTIHOOKS_SWARM_TASK") == "master"


def path(session_id: str, environ=None) -> Path:
    env = os.environ if environ is None else environ
    home = Path(env.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")
    return home / "master_coding" / (re.sub(r"[^A-Za-z0-9_.@-]", "_", session_id) + ".json")


def observe(session_id: str, prompt: str, typed: bool, environ=None, now=None) -> str:
    """Open or close the window on the operator's typed phrase in the master pane; returns open, closed or ''."""
    if not (session_id and typed and is_master(environ)):
        return ""
    target = path(session_id, environ)
    if CLOSE.search(prompt or ""):
        target.unlink(missing_ok=True)
        return "closed"
    if not OPEN.search(prompt or ""):
        return ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"at": time.time() if now is None else now}))
    return "open"


def is_open(session_id: str, environ=None, now=None) -> bool:
    if not (session_id and is_master(environ)):
        return False
    try:
        opened = json.loads(path(session_id, environ).read_text())["at"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (time.time() if now is None else now) - opened < WINDOW_SEC


def asks_operator(tool_name: str, session_id: str, environ=None, now=None) -> bool:
    """Whether a permission prompt for this tool may reach the operator present in the master pane."""
    from hooks.context.operator_mode import _heard

    return tool_name in PERMISSION_TOOLS and is_master(environ) and _heard(session_id, environ, now)


def protection_refusal(command: str, environ=None) -> str:
    return PROTECTION_REFUSAL if is_master(environ) and PROTECTION.search(command or "") else ""
