from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from hooks.lifecycle.guard import _home

STAMP_TTL_SECONDS = 6 * 3600


def kick(home: Path | None = None) -> bool:
    """Start `agentihooks deps ensure` detached when the last full check is stale."""
    try:
        ok_at = json.loads(((home or _home()) / "deps.stamp").read_text()).get("ok_at", 0)
    except (OSError, ValueError):
        ok_at = 0
    exe = shutil.which("agentihooks")
    if time.time() - ok_at < STAMP_TTL_SECONDS or exe is None:
        return False
    subprocess.Popen(  # noqa: S603
        [exe, "deps", "ensure", "--quiet"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True
