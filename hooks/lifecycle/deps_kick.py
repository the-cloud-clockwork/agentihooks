from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from hooks.lifecycle.guard import _home

STAMP_TTL_SECONDS = 6 * 3600
WAIT_SECONDS = 1.0


def kick(home: Path | None = None) -> bool:
    """Run a stale `agentihooks deps ensure` for up to WAIT_SECONDS, then kill its process group."""
    try:
        ok_at = json.loads(((home or _home()) / "deps.stamp").read_text()).get("ok_at", 0)
    except (OSError, ValueError):
        ok_at = 0
    exe = shutil.which("agentihooks")
    if time.time() - ok_at < STAMP_TTL_SECONDS or exe is None:
        return False
    process = subprocess.Popen(  # noqa: S603
        [exe, "deps", "ensure", "--quiet"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=WAIT_SECONDS)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    return True
