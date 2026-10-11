from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from hooks.common import log
from hooks.lifecycle.guard import _home

FLAG = "AGENTIHOOKS_AICODE_TOKEN_REFRESH"
INTERVAL_ENV = "AGENTIHOOKS_AICODE_TOKEN_REFRESH_INTERVAL_H"
DEFAULT_INTERVAL_H = 6.0
STAMP = "codex-refresh.stamp"


def _interval_s(environ: Mapping[str, str]) -> float:
    try:
        return float(environ.get(INTERVAL_ENV) or DEFAULT_INTERVAL_H) * 3600
    except ValueError:
        return DEFAULT_INTERVAL_H * 3600


def _due(stamp: Path, environ: Mapping[str, str]) -> bool:
    try:
        return time.time() - stamp.stat().st_mtime >= _interval_s(environ)
    except OSError:
        return True


def _start(home: Path, environ: Mapping[str, str]) -> bool:
    if environ.get(FLAG, "false").strip().lower() != "true" or not _due(home / STAMP, environ):
        return False
    exe = shutil.which("agentihooks")
    if exe is None:
        return False
    home.mkdir(parents=True, exist_ok=True)
    (home / STAMP).touch()
    subprocess.Popen(  # noqa: S603
        [exe, "codex", "refresh"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True


def kick(home: Path | None = None, environ: Mapping[str, str] | None = None) -> bool:
    """Start `agentihooks codex refresh` detached on the refresh authority when the last start is stale."""
    try:
        return _start(home or _home(), os.environ if environ is None else environ)
    except Exception as exc:
        log("codex refresh kick failed", {"error": str(exc)})
        return False
