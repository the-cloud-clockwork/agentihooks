import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from hooks.targets.normalizer import codex_rollout_path
from scripts.codex_quota import TAIL_BYTES


@dataclass(frozen=True)
class CodexContext:
    used: int
    window: int


def _parse(line: str) -> CodexContext | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    payload = cast(dict[str, Any], event).get("payload") if isinstance(event, dict) else None
    info = (
        cast(dict[str, Any], payload).get("info")
        if isinstance(payload, dict) and cast(dict[str, Any], payload).get("type") == "token_count"
        else None
    )
    usage = cast(dict[str, Any], info).get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(usage, dict):
        return None
    used, window = (
        cast(dict[str, Any], usage).get("total_tokens"),
        cast(dict[str, Any], info).get("model_context_window"),
    )
    if not isinstance(used, int) or not isinstance(window, int):
        return None
    return CodexContext(used=used, window=window)


def codex_context(session_id: str) -> CodexContext | None:
    rollout = codex_rollout_path(session_id)
    if not rollout:
        return None
    path = Path(rollout)
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"token_count"' in line and (context := _parse(line)) is not None:
            return context
    return None
