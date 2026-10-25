import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from hooks.secrets import redact
from scripts.swarm.pane_evidence import ESCAPE
from scripts.targets._common import _atomic_write


@dataclass(frozen=True)
class Attempt:
    home: Path
    name: str
    task: str
    harness: str
    launched_at: int


def text(value: str | bytes | None) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""


def _validation(fields: dict) -> dict:
    if not fields.get("profile_report"):
        return {"state": "missing", "reason": "launcher did not publish a validation report"}
    try:
        return json.loads(Path(fields["profile_report"]).read_text())
    except (OSError, ValueError) as exc:
        return {"state": "unreadable", "reason": str(exc)}


def _failure(reason: str) -> str:
    if "capacity" in reason.lower():
        return "capacity"
    if "timeout" in reason or "timed out" in reason or "no canary within" in reason:
        return "timeout"
    return "failed"


def retain(
    attempt: Attempt, fields: dict, stdout: str, stderr: str, reader: Callable[[str, int], str], reason: str
) -> Path:
    screen, screen_error = "", ""
    pane = fields.get("pane_id", "")
    if pane:
        try:
            screen = reader(pane, 80)
        except (OSError, RuntimeError, ValueError) as exc:
            screen_error = str(exc)
    validation = _validation(fields)
    row = {
        "agent": attempt.name,
        "task": attempt.task,
        "harness": attempt.harness,
        "launched_at": attempt.launched_at,
        "pane_id": pane,
        "failure": _failure("\n".join((reason, stderr, str(validation.get("reason", "")), screen))),
        "validation": validation,
        "error": "\n".join((reason, stderr)).strip(),
        "stdout": stdout,
        "stderr": stderr,
        "screen": screen,
        "screen_error": screen_error,
    }
    directory = attempt.home / "launch-evidence"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{attempt.launched_at}-{uuid4().hex}.json"
    safe = {
        key: redact(ESCAPE.sub("", value), mode="strict") if isinstance(value, str) else value
        for key, value in row.items()
    }
    _atomic_write(path, redact(json.dumps(safe), mode="strict") + "\n")
    path.chmod(0o600)
    return path
