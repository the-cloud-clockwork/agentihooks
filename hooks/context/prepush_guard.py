import os
import subprocess
from pathlib import Path

from hooks.common import log
from hooks.hook_manager import BlockAction

_PASS_ARGS = frozenset({"--delete", "-d", "--dry-run", "-n"})


def _push(tokens: list[str]) -> tuple[list[str], list[str]] | None:
    if not tokens or Path(tokens[0]).name != "git":
        return None
    dirs, rest = [], tokens[1:]
    while rest and rest[0].startswith("-"):
        if rest[0] in ("-C", "-c"):
            dirs += rest[1:2] if rest[0] == "-C" else []
            rest = rest[1:]
        rest = rest[1:]
    return (dirs, rest[1:]) if rest[:1] == ["push"] else None


def _exempt(args: list[str]) -> bool:
    if _PASS_ARGS.intersection(args):
        return True
    refspecs = [arg for arg in args if not arg.startswith("-")][1:]
    return bool(refspecs) and all(
        spec.startswith(":") or spec.split(":")[-1].removeprefix("refs/heads/").startswith("diffcheck/")
        for spec in refspecs
    )


def _toplevel(cwd: Path) -> Path | None:
    result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def _path(word: str) -> str:
    return os.path.expanduser(os.path.expandvars(word))


def _swarm_cwd(cwd: Path) -> Path:
    from scripts.gates.base import Who
    from scripts.swarm.snapshot import worktrees
    from scripts.swarm_ledger.repository.sqlite import read_ledger

    who = Who.from_env()
    if not (who.pinned and who.task):
        return cwd
    folder = Path(os.environ.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()
    doc = read_ledger(folder, who.swarm, f"tasks/{who.task}") or {}
    task = next((row for row in doc.get("tasks", []) if row.get("id") == who.task), {})
    if task.get("claimed_by") != who.name or task.get("state") not in ("claimed", "pr"):
        return cwd
    branch = task.get("branch")
    if not branch:
        return cwd
    target = worktrees(str(cwd), [branch])[branch]
    return Path(target) if target else cwd


def _unpassed(payload: dict) -> Path | None:
    from hooks.context.shell_commands import commands
    from scripts.ci_prepush import passed

    tool_input = payload["tool_input"]
    command = tool_input.get("command") or tool_input.get("cmd")
    if not command:
        return None
    cwd = Path(payload.get("cwd") or os.getcwd(), _path(tool_input.get("workdir") or ""))
    selected = bool(tool_input.get("workdir"))
    for tokens in commands(command):
        if Path(tokens[0]).name == "cd":
            target = cwd / _path(next((word for word in tokens[1:] if not word.startswith("-")), "~"))
            cwd = target if os.path.isdir(target) else cwd
            selected = True
            continue
        push = _push(tokens)
        if push is None or _exempt(push[1]):
            continue
        base = cwd if selected or push[0] else _swarm_cwd(cwd)
        root = _toplevel(base.joinpath(*map(_path, push[0])))
        if root and (root / "scripts" / "ci_prepush" / "__init__.py").is_file() and not passed(root):
            return root
    return None


def check_prepush(payload: dict) -> None:
    try:
        root = _unpassed(payload)
    except Exception as exc:
        log("prepush_guard failed", {"error": str(exc)})
        return
    if root:
        raise BlockAction(
            f"BLOCKED: HEAD in {root} has not passed `python -m scripts.ci_prepush`. "
            "Run it there, fix what fails, commit, and push once it passes."
        )
