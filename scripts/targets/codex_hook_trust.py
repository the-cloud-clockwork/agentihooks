"""Codex hook trust hashes, computed the way Codex 0.162.1 hook discovery computes them.

Codex runs a hooks.json entry only when `hooks.state."<hooks path>:<event>:<group>:<handler>".trusted_hash`
equals the sha256 of the handler's normalized identity, serialized as JSON with sorted keys. A Codex release
that changes that identity needs tests/codex_hook_fixtures.py regenerated from its app-server hooks/list.
"""

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

EVENT_LABELS = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "Stop": "stop",
    "Interrupt": "interrupt",
}
UNMATCHED_EVENTS = frozenset({"UserPromptSubmit", "Stop", "Interrupt"})
CONTEXT_EVENTS = frozenset({"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart"})
SHORT_EVENTS = frozenset({"SessionEnd", "Interrupt"})
DEFAULT_CONTEXT_LIMIT = 2500


def _timeout(event: str, value: int | None) -> int:
    if event in SHORT_EVENTS:
        return min(max(1 if value is None else value, 1), 3)
    return max(600 if value is None else value, 1)


def hook_hash(event: str, matcher: str | None, hook: dict) -> str:
    handler = {
        "type": "command",
        "command": hook["command"],
        "timeout": _timeout(event, hook.get("timeout")),
        "async": bool(hook.get("async")),
    }
    if hook.get("statusMessage") is not None:
        handler["statusMessage"] = hook["statusMessage"]
    limit = hook.get("additionalContextLimit") if event in CONTEXT_EVENTS else None
    if limit is not None and limit != DEFAULT_CONTEXT_LIMIT:
        handler["additionalContextLimit"] = limit
    identity = {"event_name": EVENT_LABELS[event], "hooks": [handler]}
    if matcher is not None and event not in UNMATCHED_EVENTS:
        identity["matcher"] = matcher
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _command_hooks(hooks: Path) -> Iterator[tuple[str, int, int, str | None, dict]]:
    try:
        doc = json.loads(hooks.read_text())
    except (OSError, ValueError):
        return
    events = doc.get("hooks") if isinstance(doc, dict) else None
    for event, groups in (events if isinstance(events, dict) else {}).items():
        if event not in EVENT_LABELS or not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            for handler_index, hook in enumerate(handlers if isinstance(handlers, list) else []):
                if isinstance(hook, dict) and hook.get("type") == "command" and str(hook.get("command", "")).strip():
                    yield event, group_index, handler_index, group.get("matcher"), hook


def trusted_state(hooks: Path, key_path: Path) -> dict[str, dict[str, str]]:
    return {
        f"{key_path}:{EVENT_LABELS[event]}:{group_index}:{handler_index}": {
            "trusted_hash": hook_hash(event, matcher, hook)
        }
        for event, group_index, handler_index, matcher, hook in _command_hooks(hooks)
    }
