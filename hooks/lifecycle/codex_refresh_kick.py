from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from hooks.common import log
from hooks.lifecycle.guard import _home
from scripts.aicode_tokens import refresh_authority

INTERVAL_ENV = "AGENTIHOOKS_AICODE_TOKEN_REFRESH_INTERVAL_H"
DEFAULT_INTERVAL_H = 6.0
HOUR_S = 3600
STAMP = "codex-refresh.stamp"
FAILED = "codex refresh kick failed"
NO_BINARY = "codex refresh kick skipped: agentihooks is not on PATH"
DETACHED = {
    "stdin": subprocess.DEVNULL,
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
    "start_new_session": True,
}


def _interval_h(environ: Mapping[str, str]) -> float:
    try:
        return float(environ.get(INTERVAL_ENV) or DEFAULT_INTERVAL_H)
    except ValueError:
        return DEFAULT_INTERVAL_H


def _due(stamp: Path, environ: Mapping[str, str]) -> bool:
    try:
        age = time.time() - stamp.stat().st_mtime
    except OSError:
        return True
    return age >= _interval_h(environ) * HOUR_S


def _start(home: Path, environ: Mapping[str, str]) -> bool:
    stamp = home / STAMP
    if not refresh_authority(environ) or not _due(stamp, environ):
        return False
    exe = shutil.which("agentihooks")
    if exe is None:
        log(NO_BINARY, {})
        return False
    home.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    try:
        process = subprocess.Popen([exe, "codex", "refresh"], **DETACHED)
    except OSError:
        stamp.unlink()
        raise
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1.0)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    return True


def kick(home: Path | None = None, environ: Mapping[str, str] | None = None) -> bool:
    try:
        return _start(home or _home(), os.environ if environ is None else environ)
    except Exception as exc:
        log(FAILED, {"error": str(exc)})
        return False
