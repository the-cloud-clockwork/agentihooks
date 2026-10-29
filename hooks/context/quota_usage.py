import json
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from hooks.config import AGENTIHOOKS_HOME, QUOTA_USAGE_STALE_SEC, QUOTA_USAGE_TOOL_CALLS
from hooks.context.account_sessions import INTERACTIVE_PREFIX

_TITLE = "ATTENTION TO QOUTA USAGE"


def _snapshot_path(session_id: str) -> Path:
    safe_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in session_id)
    return AGENTIHOOKS_HOME / "quota_usage" / f"{safe_id}.json"


def record_rate_limits(session_id: str, rate_limits: dict, environ: Mapping[str, str] = os.environ) -> None:
    if not session_id or not isinstance(rate_limits, dict):
        return
    windows = {}
    for name in ("five_hour", "seven_day"):
        window = rate_limits.get(name)
        if not isinstance(window, dict) or not isinstance(window.get("used_percentage"), (int, float)):
            continue
        windows[name] = {
            "used_percentage": float(window["used_percentage"]),
            "resets_at": window.get("resets_at"),
        }
    if not windows:
        return
    snapshot = {"updated_at": time.time(), "rate_limits": windows}
    interactive = {
        name.removeprefix(INTERACTIVE_PREFIX)
        for name, value in environ.items()
        if value and name.startswith(INTERACTIVE_PREFIX) and name != INTERACTIVE_PREFIX
    }
    if len(interactive) == 1:
        snapshot["account"] = interactive.pop()
    path = _snapshot_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(snapshot), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _snapshots(account: str) -> list[tuple[float, dict]]:
    found = []
    for path in (AGENTIHOOKS_HOME / "quota_usage").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            updated = float(data["updated_at"])
            if data.get("account") == account and isinstance(data.get("rate_limits"), dict):
                found.append((updated, data["rate_limits"]))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return found


def account_snapshot(account: str, now: float) -> tuple[float, dict] | None:
    fresh = [entry for entry in _snapshots(account) if now - entry[0] <= QUOTA_USAGE_STALE_SEC]
    return max(fresh, key=lambda entry: entry[0], default=None)


def latest_snapshot(account: str) -> tuple[float, dict] | None:
    return max(_snapshots(account), key=lambda entry: entry[0], default=None)


def clear_session_state(session_id: str) -> None:
    if session_id:
        _snapshot_path(session_id).unlink(missing_ok=True)


def _load_native(session_id: str) -> dict | None:
    if not session_id:
        return None
    try:
        data = json.loads(_snapshot_path(session_id).read_text(encoding="utf-8"))
        if time.time() - float(data["updated_at"]) > QUOTA_USAGE_STALE_SEC:
            return None
        rate_limits = data.get("rate_limits")
        return rate_limits if isinstance(rate_limits, dict) else None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _load_router_cache() -> dict | None:
    accounts = [name for name, value in os.environ.items() if name.startswith("AH_CC_TOKEN_") and value]
    if len(accounts) != 1:
        return None
    try:
        data = json.loads((AGENTIHOOKS_HOME / "claude-router-cache.json").read_text(encoding="utf-8"))
        modes = data.get("modes") or {}
        for mode in ("fable", "normal"):
            entry = ((modes.get(mode) or {}).get("accounts") or {}).get(accounts[0])
            if not isinstance(entry, dict) or time.time() - float(entry["observed_at"]) > QUOTA_USAGE_STALE_SEC:
                continue
            result = entry.get("result") or {}
            return {
                "five_hour": {
                    "used_percentage": (result.get("five_hour") or {}).get("used"),
                    "resets_at": (result.get("five_hour") or {}).get("resets_at"),
                },
                "seven_day": {
                    "used_percentage": (result.get("seven_day") or {}).get("used"),
                    "resets_at": (result.get("seven_day") or {}).get("resets_at"),
                },
            }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return None


def _reset_text(epoch: object) -> str:
    if not isinstance(epoch, (int, float)):
        return "reset unknown"
    seconds = max(0, int(epoch - time.time()))
    if seconds < 3600:
        return f"resets in {seconds // 60}m"
    if seconds < 86400:
        hours, remainder = divmod(seconds // 60, 60)
        return f"resets in {hours}h{remainder:02d}m"
    return f"resets {datetime.fromtimestamp(epoch, tz=timezone.utc).strftime('%a %H:%M UTC')}"


def _window_line(label: str, window: object) -> str | None:
    if not isinstance(window, dict) or not isinstance(window.get("used_percentage"), (int, float)):
        return None
    used = max(0.0, min(100.0, float(window["used_percentage"])))
    return f"{label} REMAINING: {100.0 - used:.0f}% | {_reset_text(window.get('resets_at'))}"


def quota_banner(session_id: str) -> str | None:
    rate_limits = _load_native(session_id) or _load_router_cache()
    if not rate_limits:
        return None
    lines = [
        line
        for line in (
            _window_line("5H", rate_limits.get("five_hour")),
            _window_line("7D", rate_limits.get("seven_day")),
        )
        if line
    ]
    if len(lines) != 2:
        return None
    lines.append(f"Shown every {QUOTA_USAGE_TOOL_CALLS} tool calls")
    width = 78
    border = "═" * width
    title = f"║  {_TITLE}".ljust(width + 1) + "║"
    body = "\n".join((f"║  {line}".ljust(width + 1) + "║") for line in lines)
    return f"╔{border}╗\n{title}\n╠{border}╣\n{body}\n╚{border}╝"
