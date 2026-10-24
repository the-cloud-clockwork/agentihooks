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


def _common_dir(cwd: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _path(word: str) -> str:
    return os.path.expanduser(os.path.expandvars(word))


def _own_worktree(cwd: Path, name: str) -> Path | None:
    from scripts.gates.push_stop import default_root, trees

    checkout = _toplevel(cwd)
    if checkout is None:
        return None
    found = [tree.path for tree in trees(default_root(), name) if tree.path.parent.name == checkout.name]
    return found[0] if len(found) == 1 else None


def _branch_worktree(cwd: Path, task: dict) -> Path | None:
    from scripts.gates.push_stop import default_root
    from scripts.swarm.snapshot import worktrees

    branch = task["branch"]
    candidates = [cwd]
    if task.get("worktree"):
        candidates.append(Path(task["worktree"]))
    candidates.extend(path for path in Path(default_root()).glob("*/*") if (path / ".git").is_file())
    found = {Path(target) for candidate in candidates if (target := worktrees(str(candidate), [branch])[branch])}
    return found.pop() if len(found) == 1 else None


def _swarm_cwd(cwd: Path) -> tuple[Path | None, str]:
    from scripts.gates.base import Who
    from scripts.swarm_ledger.storage import read_ledger

    who = Who.from_env()
    if not (who.pinned and who.task):
        return None, "identity binding"
    folder = Path(os.environ.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()
    doc = read_ledger(folder, who.swarm, f"tasks/{who.task}") or {}
    task = next((row for row in doc.get("tasks", []) if row.get("id") == who.task), {})
    if not task:
        return None, "ledger task"
    if task.get("claimed_by") != who.name or task.get("state") not in ("claimed", "pr"):
        return None, "claimant or task state"
    target = _branch_worktree(cwd, task) if task.get("branch") else _own_worktree(cwd, who.name)
    return target, "worktree lookup"


def _require_directory(target: Path | None, stage: str) -> Path | None:
    from hooks.targets import is_codex

    if target is None and is_codex():
        raise BlockAction(
            f"BLOCKED: Could not resolve the push directory ({stage}). "
            "Use git -C /absolute/path/to/worktree push with the intended repository."
        )
    return target


def _push_directory(cwd: Path, selected: bool) -> Path:
    from hooks.targets.capabilities import reports_shell_cwd

    if selected:
        return cwd
    target, stage = _swarm_cwd(cwd)
    if target is not None and reports_shell_cwd() and _common_dir(target) != _common_dir(cwd):
        return cwd
    return _require_directory(target, stage) or cwd


def _folder(cwd: Path, folders: list[str]) -> Path:
    previous = cwd
    for folder in folders:
        target = previous if folder == "-" else cwd / _path(folder)
        if os.path.isdir(target):
            cwd, previous = target, cwd
    return cwd


def _unpassed(payload: dict) -> Path | None:
    from hooks.context.shell_commands import located
    from scripts.ci_prepush import passed

    tool_input = payload["tool_input"]
    command = tool_input.get("command") or tool_input.get("cmd")
    if not command:
        return None
    cwd = Path(payload.get("cwd") or os.getcwd(), _path(tool_input.get("workdir") or ""))
    selected = bool(tool_input.get("workdir"))
    for tokens, folders in located(command):
        push = _push(tokens)
        if push is None or _exempt(push[1]):
            continue
        base = _push_directory(_folder(cwd, folders), selected or bool(folders) or bool(push[0]))
        root = _require_directory(_toplevel(base.joinpath(*map(_path, push[0]))), "repository lookup")
        if root and (root / "scripts" / "ci_prepush" / "__init__.py").is_file() and not passed(root):
            return root
    return None


def check_prepush(payload: dict) -> None:
    import shlex

    try:
        root = _unpassed(payload)
    except BlockAction:
        raise
    except Exception as exc:
        log("prepush_guard failed", {"error": str(exc)})
        return
    if root is None:
        return
    command = "python -m scripts.ci_prepush"
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
        )
        dirty = status.returncode == 0 and bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        dirty = False
    if dirty:
        command = "git status --short"
    raise BlockAction(
        f"BLOCKED: HEAD in {root} has not passed `python -m scripts.ci_prepush`. "
        "Commit intended changes first, run the gate on that commit, then push. "
        f"Next: cd {shlex.quote(str(root))} && {command}"
    )
