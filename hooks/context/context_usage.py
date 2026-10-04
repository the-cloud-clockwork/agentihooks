import json
import os
import time
from pathlib import Path

from hooks.config import AGENTIHOOKS_HOME


def _snapshot_path(session_id: str) -> Path:
    safe_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in session_id)
    return AGENTIHOOKS_HOME / "context_usage" / f"{safe_id}.json"


def record_context_usage(session_id: str, context_window: dict) -> None:
    if not session_id or not isinstance(context_window, dict):
        return
    size = context_window.get("context_window_size")
    used_pct = context_window.get("used_percentage")
    if not isinstance(size, (int, float)) or not isinstance(used_pct, (int, float)) or size <= 0:
        return
    path = _snapshot_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    snapshot = {
        "used_tokens": int(size * used_pct / 100),
        "context_window_size": int(size),
        "used_pct": float(used_pct),
        "updated_at": time.time(),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(snapshot), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def used_tokens(session_id: str) -> int | None:
    if not session_id:
        return None
    try:
        return int(json.loads(_snapshot_path(session_id).read_text(encoding="utf-8"))["used_tokens"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
