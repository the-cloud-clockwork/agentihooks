import os
import subprocess
from pathlib import Path

from hooks.common import log
from hooks.hook_manager import BlockAction

_PREFIXES = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "time", "exec", "nohup", "env", "command", "!", "{"}
)
_PASS_ARGS = frozenset({"--delete", "-d", "--dry-run", "-n"})


def _push(tokens: list[str]) -> tuple[list[str], list[str]] | None:
    while tokens and (tokens[0] in _PREFIXES or "=" in tokens[0] or tokens[0] == "timeout"):
        tokens = tokens[2:] if tokens[0] == "timeout" else tokens[1:]
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


def _unpassed(payload: dict) -> Path | None:
    from hooks.context._strip import strip_non_command_content
    from hooks.context.branch_guard import _command_lines, _resolve_cwd
    from scripts.ci_prepush import passed

    command = payload["tool_input"]["command"]
    cwd = Path(_resolve_cwd(command, payload.get("cwd")))
    for line in _command_lines(strip_non_command_content(command)).splitlines():
        push = _push(line.split())
        if push is None or _exempt(push[1]):
            continue
        root = _toplevel(cwd.joinpath(*(os.path.expanduser(os.path.expandvars(path)) for path in push[0])))
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
