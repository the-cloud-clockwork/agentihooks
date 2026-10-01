import json
from dataclasses import replace
from pathlib import Path

from hooks.lifecycle.liveness import Snapshot
from hooks.lifecycle.model import ACTIONABLE, Finding

CONFIRM_AFTER = 3600


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def confirm(findings: list[Finding], snap: Snapshot, path: Path) -> list[Finding]:
    previous = _load(path)
    seen = previous.get("seen", {}) if previous.get("boot_id") == snap.boot_id else {}
    current, result = {}, []
    for item in findings:
        if item.action not in ACTIONABLE:
            result.append(item)
            continue
        first = seen.get(item.path, {})
        if first.get("action") != item.action:
            first = {"action": item.action, "uptime": snap.uptime}
        current[item.path] = first
        result.append(replace(item, due=snap.uptime - first["uptime"] >= CONFIRM_AFTER))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"boot_id": snap.boot_id, "seen": current}), encoding="utf-8")
    return result
