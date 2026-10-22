"""The master hotfix window: the operator's typed allow-master-coding lets the master seat code for thirty minutes.

Only a prompt the operator typed in the master pane opens or closes it; agent text, tool output and relays never do.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

WINDOW_SEC = 1800
OPEN = re.compile(r"(?<![\w-])allow-master-coding(?![\w-])")
CLOSE = re.compile(r"(?<![\w-])disallow-master-coding(?![\w-])")
PERMISSION_TOOLS = ("AskUserQuestion", "ExitPlanMode")
PROTECTION = re.compile(r"\bgh\s+(?:ruleset\b|api\b[^\n;&|]*(?:/rulesets\b|/branches/[^\s/]+/protection\b))")
NOTICES = {
    "open": "Master hotfix window open for thirty minutes: the master may create a worktree with wt.sh new, edit "
    "inside it, commit, push its branch, merge its pull request into dev on green and change rulesets or branch "
    "protection by command; a ruleset write names its committed payload file, and main, master and v1 stay "
    "closed. Condition folders, credential files and secrets stay closed. "
    "disallow-master-coding ends it.",
    "closed": "Master hotfix window closed: the master no longer edits code.",
}
CREDENTIALS = re.compile(
    r"(?:^|/)(?:\.env(?:\.[\w.-]+)?|[\w.-]+\.env|\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.pgpass)$"
)
TEMPLATES = (".example", ".sample", ".template", ".dist")
MAIN_PROTECTION = re.compile(r"(?<!\w)(?:main|master|v1)(?!\w)")
RULESET_BY_ID = re.compile(r"/rulesets/\d+")
RULESET_PAYLOAD = re.compile(r"(?<![\w/.~-])\.github/rulesets/[\w.-]+\.json")
PROTECTION_REFUSAL = (
    "BLOCKED: the master changes rulesets or branch protection only inside the hotfix window, a ruleset write "
    "only from its committed payload file under .github/rulesets, and never for main, master or v1; the operator "
    "opens the window by typing allow-master-coding in the master pane."
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
    if os.environ.get("AGENTIHOOKS_SWARM") and os.environ.get("AGENTIHOOKS_SWARM_TASK") == "master":
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
        path(session_id).unlink(missing_ok=True)
        return "closed"
    if not OPEN.search(str(prompt)):
        return ""
    path(session_id).parent.mkdir(parents=True, exist_ok=True)
    path(session_id).write_text(json.dumps({"at": time.time() if now is None else now}))
    return "open"


def is_open(session_id: str, now: float | None = None) -> bool:
    if not (session_id and is_master(session_id)):
        return False
    try:
        opened = json.loads(path(session_id).read_text())["at"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (time.time() if now is None else now) - opened < WINDOW_SEC


def closed_in_window(value: str, cwd: Path) -> bool:
    """Condition folders and credential files stay closed even while the window is open."""
    target = (cwd / Path(value).expanduser()).resolve()
    credential = CREDENTIALS.search(str(target)) and not str(target).endswith(TEMPLATES)
    return "conditions" in target.parts or bool(credential)


def asks_operator(tool_name: str, session_id: str | None) -> bool:
    """Whether a permission prompt for this tool may reach the operator, who opens it only by typing operator on."""
    from hooks.context.operator_mode import _heard

    return tool_name in PERMISSION_TOOLS and _heard(session_id)


def protection_refusal(payload: dict) -> str:
    command = str((payload.get("tool_input") or {}).get("command"))
    session_id = payload.get("session_id")
    if not (is_master(session_id) and PROTECTION.search(command)):
        return ""
    if not is_open(session_id) or MAIN_PROTECTION.search(command):
        return PROTECTION_REFUSAL
    if RULESET_BY_ID.search(command) and not _committed_payload(command, Path(payload.get("cwd") or os.getcwd())):
        return PROTECTION_REFUSAL
    return ""


def _committed_payload(command: str, cwd: Path) -> bool:
    found = RULESET_PAYLOAD.search(command)
    if not found:
        return False
    target = str((cwd / found.group(0)).resolve())
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    tracked = subprocess.run(["git", "-C", str(cwd), "ls-files", "--error-unmatch", target], **quiet)
    clean = subprocess.run(["git", "-C", str(cwd), "diff", "--quiet", "HEAD", "--", target], **quiet)
    return tracked.returncode == 0 and clean.returncode == 0
