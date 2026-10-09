"""Prepush Guard — refuses `git push` of a HEAD that has not passed `python -m scripts.ci_prepush`.

Only repositories that ship scripts/ci_prepush are guarded. Deletes, dry runs and diffcheck plant
branches pass: a plant is meant to fail the gates.
"""

import re
import subprocess
from pathlib import Path

from hooks.hook_manager import BlockAction

_PUSH_RE = re.compile(r"^git(?:\s+-C\s+(\S+))?(?:\s+-\S+)*\s+push\b(.*)$")
_PASS_ARGS = frozenset({"--delete", "-d", "--dry-run", "-n"})


def _toplevel(cwd: Path) -> Path | None:
    result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def _exempt(args: list[str]) -> bool:
    return any(arg in _PASS_ARGS or arg.startswith(":") or "diffcheck/" in arg for arg in args)


def check_prepush(payload: dict) -> None:
    from hooks.context._strip import strip_non_command_content
    from hooks.context.branch_guard import _command_lines, _resolve_cwd
    from scripts.ci_prepush import passed

    command = payload.get("tool_input", {}).get("command", "")
    if not command:
        return
    cwd = Path(_resolve_cwd(command, payload.get("cwd", "")))
    for line in _command_lines(strip_non_command_content(command)).splitlines():
        match = _PUSH_RE.match(line.strip())
        if not match or _exempt(match.group(2).split()):
            continue
        root = _toplevel(cwd / match.group(1) if match.group(1) else cwd)
        if root is None or not (root / "scripts" / "ci_prepush" / "__init__.py").is_file() or passed(root):
            continue
        raise BlockAction(
            f"BLOCKED: HEAD in {root} has not passed the cheap CI gates. Run `python -m scripts.ci_prepush` "
            "there (lint, format, size limits and the touched tests, two workers, no Redis), fix what fails, "
            "commit, and push once it reports every gate passed."
        )
