"""The master hotfix window: the operator's typed allow-master-coding lets the master seat code until the operator types
deny-master-coding or disallow-master-coding, or the session restarts.

Only a prompt the operator typed in the master pane opens or closes it; agent text, tool output and relays never do.
"""

import json
import os
import re
import time
from pathlib import Path

OPEN = re.compile(r"(?<![\w-])allow-master-coding(?![\w-])")
CLOSE = re.compile(r"(?<![\w-])(?:deny|disallow)-master-coding(?![\w-])")
PERMISSION_TOOLS = ("AskUserQuestion", "ExitPlanMode")
MASTER_TASK = re.compile(r"master(?:-\d+)?")
PROTECTION = re.compile(r"\bgh\s+(?:ruleset\b|api\b[^\n;&|]*(?:/rulesets\b|/branches/[^\s/]+/protection\b))")
NOTICES = {
    "open": "Master hotfix window open until deny-master-coding or a session restart: the master may create a worktree with wt.sh new, edit "
    "inside it, commit, push its branch and merge its pull request into dev on green. Main, rulesets, branch "
    "protection, condition folders, credential files and secrets stay closed. deny-master-coding ends it.",
    "closed": "Master hotfix window closed: the master no longer edits code.",
}
CREDENTIALS = re.compile(
    r"(?:^|/)(?:\.env(?:\.[\w.-]+)?|[\w.-]+\.env|\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.pgpass)$"
)
TEMPLATES = (".example", ".sample", ".template", ".dist")
PROTECTION_REFUSAL = (
    "BLOCKED: the master never changes rulesets or branch protection, inside the hotfix window or out of it."
)


def seat_path(session_id: str) -> Path:
    home = Path(os.environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")
    return home / "master_seats" / (re.sub(r"[^A-Za-z0-9_.@-]", "_", session_id) + ".json")


def seat(session_id: str | None) -> dict:
    if not session_id:
        return {}
    try:
        data = json.loads(seat_path(session_id).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_seat(session_id: str, slug: str, name: str) -> None:
    target = seat_path(session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"slug": slug, "name": name}))


def is_master(session_id: str | None = None) -> bool:
    if os.environ.get("AGENTIHOOKS_SWARM") and MASTER_TASK.fullmatch(str(os.environ.get("AGENTIHOOKS_SWARM_TASK"))):
        return True
    return bool(session_id) and seat_path(session_id).is_file()


def path(session_id: str) -> Path:
    home = Path(os.environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")
    return home / "master_coding" / (re.sub(r"[^A-Za-z0-9_.@-]", "_", session_id) + ".json")


def observe(session_id: str | None, prompt: str | None, typed: bool, now: float | None = None) -> str:
    """Open or close the window on the operator's typed phrase in the master pane; returns open, closed or ''."""
    if not (session_id and typed and is_master(session_id)):
        return ""
    if CLOSE.search(str(prompt)):
        close(session_id)
        return "closed"
    if not OPEN.search(str(prompt)):
        return ""
    path(session_id).parent.mkdir(parents=True, exist_ok=True)
    path(session_id).write_text(json.dumps({"at": time.time() if now is None else now}))
    return "open"


def close(session_id: str | None) -> None:
    if session_id:
        path(session_id).unlink(missing_ok=True)


def is_open(session_id: str) -> bool:
    return bool(session_id) and is_master(session_id) and path(session_id).is_file()


def closed_in_window(value: str, cwd: Path) -> bool:
    """Condition folders and credential files stay closed even while the window is open."""
    target = (cwd / Path(value).expanduser()).resolve()
    credential = CREDENTIALS.search(str(target)) and not str(target).endswith(TEMPLATES)
    return "conditions" in target.parts or bool(credential)


def asks_operator(tool_name: str, session_id: str | None) -> bool:
    """Whether a permission prompt for this tool may reach the operator: always unbound, bound only after operator on."""
    from hooks.context.operator_mode import _heard

    return tool_name in PERMISSION_TOOLS and _heard(session_id)


def protection_refusal(payload: dict) -> str:
    command = str((payload.get("tool_input") or {}).get("command"))
    return PROTECTION_REFUSAL if is_master(payload.get("session_id")) and PROTECTION.search(command) else ""
