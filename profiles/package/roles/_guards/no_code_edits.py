import json
import os
import re
import sys
import time
from pathlib import Path

PLANNERS = {"master", "planner"}
HOTFIX_SEC = 1800
CREDENTIALS = re.compile(
    r"(?:^|/)(?:\.env(?:\.[\w.-]+)?|[\w.-]+\.env|\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.pgpass)$"
)
TEMPLATES = (".example", ".sample", ".template", ".dist")
BUILTIN_WRITES = {"edit", "write", "notebookedit"}
SERENA_WRITES = {
    f"mcp__serena__{name}"
    for name in (
        "replace_symbol_body",
        "insert_after_symbol",
        "insert_before_symbol",
        "rename_symbol",
        "replace_content",
        "replace_in_files",
        "safe_delete_symbol",
    )
}


def _allowed_roots(role: str, home: Path) -> list[Path]:
    roots = [(home / "scratchpad").resolve()]
    if role in PLANNERS:
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        roots.append((config / "plans").resolve())
    swarm = os.environ.get("AGENTIHOOKS_SWARM", "")
    task = os.environ.get("AGENTIHOOKS_SWARM_TASK", "")
    if task != "master" and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value) for value in (swarm, task)):
        folder = home.resolve() / ".agentihooks" / "swarm" / swarm / "tasks" / task
        if folder.resolve() == folder:
            roots.append(folder)
    return roots


def _inside(tool: str, value: str, cwd: Path, roots: list[Path]) -> bool:
    path = Path(value).expanduser()
    if tool == "mcp__serena__rename_symbol" or (tool in SERENA_WRITES and not path.is_absolute()):
        return False
    target = (path if path.is_absolute() else cwd / path).resolve()
    return any(target.is_relative_to(root) for root in roots)


def hotfix_open(payload: dict, role: str, home: Path, now: float | None = None) -> bool:
    """The operator typed allow-master-coding in this master pane within the last thirty minutes."""
    if role != "master" or os.environ.get("AGENTIHOOKS_SWARM_TASK") != "master":
        return False
    session = re.sub(r"[^A-Za-z0-9_.@-]", "_", str(payload.get("session_id") or ""))
    agentihooks = Path(os.environ.get("AGENTIHOOKS_HOME") or home / ".agentihooks")
    try:
        opened = json.loads((agentihooks / "master_coding" / f"{session}.json").read_text())["at"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return bool(session) and (time.time() if now is None else now) - opened < HOTFIX_SEC


def _closed_in_window(value: str, cwd: Path) -> bool:
    target = (cwd / Path(value).expanduser()).resolve()
    credential = CREDENTIALS.search(str(target)) and not str(target).endswith(TEMPLATES)
    return "conditions" in target.parts or bool(credential)


def deny_reason(payload: dict, role: str, home: Path) -> str:
    tool = payload["tool_name"].lower()
    if tool not in BUILTIN_WRITES | SERENA_WRITES:
        return ""
    inputs = payload["tool_input"]
    paths = [inputs[key] for key in ("file_path", "notebook_path", "relative_path") if key in inputs]
    paths.extend(inputs.get("file_paths", []))
    cwd = Path(payload.get("cwd") or os.getcwd())
    roots = _allowed_roots(role, home)
    if hotfix_open(payload, role, home):
        if any(_closed_in_window(value, cwd) for value in paths):
            return "master hotfix window: condition folders and credential files stay closed."
        roots.append((home / "dev" / "worktrees").resolve())
    if paths and all(_inside(tool, value, cwd, roots) for value in paths):
        return ""
    return f"{role} cannot edit outside ~/scratchpad; send the change to its author."


def main(role: str) -> int:
    reason = deny_reason(json.load(sys.stdin), role, Path.home())
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0
