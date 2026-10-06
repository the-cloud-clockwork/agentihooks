from __future__ import annotations

import json
import time
from pathlib import Path

from hooks import config


def marker_path() -> Path:
    return config.AGENTIHOOKS_HOME / "classifier" / "api-down"


def is_down(ttl_s: float) -> bool:
    try:
        marked = marker_path().stat().st_mtime
    except FileNotFoundError:
        return False
    return time.time() - marked < ttl_s


def mark_down(failures: list | None = None) -> None:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(failures or []))


def failures() -> list:
    try:
        return json.loads(marker_path().read_text())
    except (OSError, ValueError):
        return []


def clear() -> None:
    marker_path().unlink(missing_ok=True)
