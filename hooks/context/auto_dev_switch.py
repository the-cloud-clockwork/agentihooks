"""Auto dev-switch — SessionStart hook.

If the session's cwd is a git repo currently on main/master, check out to dev
(or create + push dev if it doesn't exist). Keeps the agent working on dev
across repos where main is still the default branch.

Skipped when:
  - Not a git repo
  - Already on a non-main branch
  - Working tree is dirty (don't risk operator's in-progress work)

Config:
    AUTO_DEV_SWITCH_ENABLED (bool, default True)

See CI Manifesto §13 for doctrine.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hooks.common import log


def _git(args: list[str], cwd: str, timeout: int = 10) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"GIT_ALLOW_MAIN_PUSH": "1", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(Path.home())},
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 99, "", str(e)


def ensure_on_dev(cwd: str) -> str:
    """Perform the switch. Return a status message (empty if no action taken)."""
    try:
        from hooks.config import AUTO_DEV_SWITCH_ENABLED

        if not AUTO_DEV_SWITCH_ENABLED:
            return ""
    except Exception:
        pass

    if not cwd or not Path(cwd).is_dir():
        return ""

    rc, out, _ = _git(["rev-parse", "--is-inside-work-tree"], cwd)
    if rc != 0 or out != "true":
        return ""

    rc, branch, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if rc != 0 or not branch:
        return ""

    if branch not in ("main", "master"):
        return ""

    rc, status, _ = _git(["status", "--porcelain"], cwd)
    if rc == 0 and status:
        msg = f"on '{branch}' but working tree is dirty — NOT switching (commit/stash first)"
        log(f"auto_dev_switch: {msg}", {"cwd": cwd})
        return msg

    rc, _, _ = _git(["checkout", "dev"], cwd)
    if rc == 0:
        msg = f"switched {branch} → dev (existing branch)"
        log(f"auto_dev_switch: {msg}", {"cwd": cwd})
        return msg

    rc, _, err = _git(["checkout", "-b", "dev"], cwd)
    if rc != 0:
        msg = f"FAILED to create dev branch: {err[:120]}"
        log(f"auto_dev_switch: {msg}", {"cwd": cwd})
        return msg

    rc, _, err = _git(["push", "-u", "origin", "dev"], cwd, timeout=30)
    if rc != 0:
        msg = f"dev created locally but push failed: {err[:120]}"
        log(f"auto_dev_switch: {msg}", {"cwd": cwd})
        return msg

    msg = f"created dev branch from {branch} and pushed to origin"
    log(f"auto_dev_switch: {msg}", {"cwd": cwd})
    return msg


def inject_on_session_start(cwd: str) -> None:
    """Emit additionalContext notice if we switched branches."""
    try:
        msg = ensure_on_dev(cwd)
        if not msg:
            return
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": f"[auto-dev-switch] {msg}",
                    }
                }
            )
        )
    except Exception as e:
        log("auto_dev_switch inject failed", {"error": str(e)})
