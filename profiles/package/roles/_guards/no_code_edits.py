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


def deny_reason(payload: dict, role: str, home: Path) -> str:
    tool = payload["tool_name"].lower()
    if tool not in BUILTIN_WRITES | SERENA_WRITES:
        return ""
    inputs = payload["tool_input"]
    paths = [inputs[key] for key in ("file_path", "notebook_path", "relative_path") if key in inputs]
    paths.extend(inputs.get("file_paths", []))
    cwd = Path(payload.get("cwd") or os.getcwd())
    roots = _allowed_roots(role, home)
    if paths and all(_inside(tool, value, cwd, roots) for value in paths):
        return ""
    return f"{role} cannot edit outside ~/scratchpad; send the change to its author."


def main(role: str) -> int:
    reason = deny_reason(json.load(sys.stdin), role, Path.home())
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0
