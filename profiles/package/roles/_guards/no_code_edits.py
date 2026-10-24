import json
import os
import re
import sys
from pathlib import Path

PLANNERS = {"master", "planner"}
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


def _seated(session: str, agentihooks: Path) -> bool:
    if os.environ.get("AGENTIHOOKS_SWARM_TASK") == "master":
        return True
    return bool(session) and (agentihooks / "master_seats" / f"{session}.json").is_file()


def hotfix_open(payload: dict, role: str) -> bool:
    """The operator typed allow-master-coding in this master session and has not closed it."""
    if role != "master":
        return False
    from hooks.context import master_coding

    return master_coding.is_open(str(payload.get("session_id") or ""))


def _delegated(payload: dict, role: str, home: Path) -> bool:
    session = re.sub(r"[^A-Za-z0-9_.@-]", "_", str(payload.get("session_id") or ""))
    agentihooks = Path(os.environ.get("AGENTIHOOKS_HOME") or home / ".agentihooks")
    return role == "master" and bool(payload.get("agent_id")) and _seated(session, agentihooks)


def deny_reason(payload: dict, role: str, home: Path) -> str:
    tool = payload["tool_name"].lower()
    if tool not in BUILTIN_WRITES | SERENA_WRITES:
        return ""
    inputs = payload["tool_input"]
    paths = [inputs[key] for key in ("file_path", "notebook_path", "relative_path") if key in inputs]
    paths.extend(inputs.get("file_paths", []))
    cwd = Path(payload.get("cwd") or os.getcwd())
    roots = _allowed_roots(role, home)
    if _delegated(payload, role, home) or hotfix_open(payload, role):
        from hooks.context.master_coding import closed_in_window

        if any(closed_in_window(value, cwd) for value in paths):
            return "master hotfix window: condition folders and credential files stay closed."
        roots.append((home / "dev" / "worktrees").resolve())
        relative = all(value and not Path(value).expanduser().is_absolute() for value in paths)
        if tool in SERENA_WRITES - {"mcp__serena__rename_symbol"} and paths and relative:
            return ""
    if paths and all(_inside(tool, value, cwd, roots) for value in paths):
        return ""
    return f"{role} cannot edit outside ~/scratchpad; send the change to its author."


def main(role: str) -> int:
    try:
        reason = deny_reason(json.load(sys.stdin), role, Path.home())
    except Exception:
        reason = f"{role} cannot evaluate the coding window; edits stay closed."
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0
