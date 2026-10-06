from __future__ import annotations

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


def mark_down() -> None:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()))


def clear() -> None:
    marker_path().unlink(missing_ok=True)
