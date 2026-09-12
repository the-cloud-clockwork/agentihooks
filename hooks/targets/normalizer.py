"""Normalize a Codex or Copilot hook payload into the shape handlers consume.

Codex cloned Claude Code's hook stdin contract almost verbatim (same
``hook_event_name``, ``tool_name``/``tool_input``/``tool_response``,
``session_id``/``transcript_path``/``cwd``), so that path is alias-filling,
not translation.

Copilot sends the same fields camelCased (``sessionId``, ``toolName``,
``toolArgs``, ``cwd``) and stores its transcript as a session-events log
rather than a rollout, so its path fills both spellings and resolves
``events.jsonl``.

Claude payloads pass through untouched so the claude behavior stays
byte-identical.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from hooks.targets import codex_home as _codex_home
from hooks.targets import copilot_home as _copilot_home
from hooks.targets import is_codex, is_copilot

# Events whose handlers read the transcript: session digest/metrics, brain
# marker capture, auto-save, compaction. Tool events deliberately excluded.
_TRANSCRIPT_EVENTS = frozenset({"SessionEnd", "Stop", "SubagentStop", "PreCompact"})


# A codex session id is a UUID; anything else is refused rather than
# interpolated into a glob, where `*`/`?`/`[` would match another session's
# rollout and `**` would raise ValueError out of the hook process.
_SESSION_ID_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# Rollouts live under sessions/YYYY/MM/DD. A hook only ever asks about the
# session it is running in, so the newest day directories hold it; walking
# them newest-first turns the common case into one directory scan instead of
# a walk of the operator's entire session history.
_RECENT_DAY_DIRS = 8


def _day_dirs_newest_first(sessions: Path) -> list[Path]:
    days: list[Path] = []
    try:
        for year in sorted((d for d in sessions.iterdir() if d.is_dir()), reverse=True):
            for month in sorted((d for d in year.iterdir() if d.is_dir()), reverse=True):
                days.extend(sorted((d for d in month.iterdir() if d.is_dir()), reverse=True))
                if len(days) >= _RECENT_DAY_DIRS:
                    return days[:_RECENT_DAY_DIRS]
    except OSError:
        return days[:_RECENT_DAY_DIRS]
    return days[:_RECENT_DAY_DIRS]


def codex_rollout_path(session_id: str) -> str:
    """Locate the rollout jsonl for *session_id*, or "" if there is none.

    Codex omits ``transcript_path`` from its hook payloads, so every
    transcript-driven feature (brain markers, auto-save, tool-memory error
    scanning, session metrics) would silently no-op. Rollouts are written to
    ``<codex home>/sessions/YYYY/MM/DD/rollout-<stamp>-<session id>.jsonl``,
    and the session id is unique, so the first match is the file.

    Never raises: a hook that dies here would skip every guardrail for that
    event, so any lookup failure degrades to "no transcript".
    """
    if not session_id or not _SESSION_ID_SAFE.fullmatch(session_id):
        return ""
    sessions = _codex_home() / "sessions"
    pattern = f"rollout-*{glob.escape(session_id)}.jsonl"
    try:
        if not sessions.is_dir():
            return ""
        for day in _day_dirs_newest_first(sessions):
            match = next(day.glob(pattern), None)
            if match:
                return str(match)
        # Older than the recent window (a long-dormant session resumed today
        # still writes into today's directory, so this is the rare path).
        match = next(sessions.glob(f"**/{pattern}"), None)
        return str(match) if match else ""
    except (OSError, ValueError, RuntimeError):
        return ""


def copilot_events_path(session_id: str) -> str:
    """Locate the session-events jsonl for *session_id*, or "" if there is none.

    Copilot writes ``<copilot home>/session-state/<session id>/events.jsonl``
    and omits ``transcript_path`` from hook payloads, so every
    transcript-driven feature would silently no-op without this. The session
    id is a path segment here, not a glob, so it is validated against the same
    charset the codex resolver uses before being joined.

    Never raises: a hook that dies here would skip every guardrail for that
    event, so any lookup failure degrades to "no transcript".
    """
    if not session_id or not _SESSION_ID_SAFE.fullmatch(session_id):
        return ""
    try:
        events = _copilot_home() / "session-state" / session_id / "events.jsonl"
        return str(events) if events.is_file() else ""
    except (OSError, ValueError, RuntimeError):
        return ""


# Copilot dispatches its own camelCase event names and also accepts the
# Claude-style PascalCase aliases at registration. Which spelling comes back in
# the payload is not contractual — and the adapter registers the PascalCase
# ones — so BOTH must resolve. An unmapped name reaches no handler and exits 0,
# silently bypassing every guardrail for that event, so the identity entries
# below are load-bearing, not cosmetic.
#
# postToolUseFailure has no distinct handler — it is the failure arm of
# PostToolUse and carries the same payload plus an error, so folding it in is
# what lets tool-error recording see copilot failures at all.
#
# postResult and prePRDescription are deliberately absent: the adapter does not
# register them, and folding them onto Stop would re-run session-end work once
# per result. hook_manager logs one as an unknown event if it ever arrives.
_COPILOT_EVENTS = {
    "sessionStart": "SessionStart",
    "sessionEnd": "SessionEnd",
    "userPromptSubmitted": "UserPromptSubmit",
    "userPromptTransformed": "UserPromptSubmit",
    "preToolUse": "PreToolUse",
    "preMcpToolCall": "PreToolUse",
    "postToolUse": "PostToolUse",
    "postToolUseFailure": "PostToolUse",
    "agentStop": "Stop",
    "subagentStart": "SubagentStart",
    "subagentStop": "SubagentStop",
    "preCompact": "PreCompact",
    "permissionRequest": "PermissionRequest",
    "notification": "Notification",
    "errorOccurred": "Notification",
}
# Identity entries so the PascalCase spelling the adapter registers under
# resolves too. Derived, not hand-listed: a value added above cannot be
# forgotten here.
_COPILOT_EVENTS.update({v: v for v in list(_COPILOT_EVENTS.values())})
# PascalCase names the adapter registers that are NOT their own dispatch name,
# so the derived identity pass above cannot cover them.
_COPILOT_EVENTS["PostToolUseFailure"] = "PostToolUse"


# Copilot's hook-boundary tool names → the Claude names every guardrail
# switches on (observed live, v1.0.80: `bash`, `view`, `create`, `edit`,
# `glob`). An unmapped name (an MCP tool) passes through unchanged. `shell`
# is the docs' name for the shell tool; the hook boundary says `bash` — both
# kept so a rename in either direction cannot unmap the one tool the
# mutation guards care about.
_COPILOT_TOOL_NAMES = {
    "bash": "Bash",
    "shell": "Bash",
    "view": "Read",
    "create": "Write",
    "edit": "Edit",
    "grep": "Grep",
    "glob": "Glob",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "task": "Agent",
    # Two Rust-backed builtin write tools the v1.0.80 binary dispatches that
    # are NOT single verbs: `apply_patch` carries a whole patch body in
    # {patch: ...}; `str_replace_editor` is a dispatcher whose real verb is a
    # nested `command` field. Both are in copilot's own write-tool set
    # (`new Set(["apply_patch","create","edit","str_replace"])`), so a model
    # backend that emits them (OpenAI/Anthropic tool conventions) would slip a
    # write past the Write/Edit secrets branch unless it maps to one. Handled
    # by name below AND by unpacking their args into a field the scan reads.
    "apply_patch": "Edit",
    "str_replace_editor": "Edit",
}

# str_replace_editor's nested command → the Claude tool it really is.
_COPILOT_STR_REPLACE_VERBS = {
    "create": "Write",
    "str_replace": "Edit",
    "insert": "Edit",
    "view": "Read",
    "undo_edit": "Edit",
}

# Copilot arg keys → the Claude spellings the scanners read
# (`create` sends {path, file_text}; `edit` sends {path, old_str, new_str}).
# Filled alongside the originals, never replacing them.
_COPILOT_ARG_ALIASES = (
    ("path", "file_path"),
    ("file_text", "content"),
    ("old_str", "old_string"),
    ("new_str", "new_string"),
)


def _copilot_tool_call(name: str, args: Any) -> tuple[str, dict[str, Any]]:
    """Map one copilot tool call onto the (tool_name, tool_input) handlers read.

    ``args`` arrives as a JSON STRING at the hook boundary. If it does not
    parse, the raw text is placed under both ``command`` and ``content`` so
    the secrets scan still sees it — parse failure must never hide a payload
    from the HARD FLOOR.
    """
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"command": args, "content": args}
    if not isinstance(args, dict):
        args = {}

    # apply_patch's entire diff lives in {patch: "..."} — alias it into
    # `content` so the Write/Edit secrets branch, which reads content, sees
    # the whole body (a secret anywhere in the patch is caught).
    if name == "apply_patch" and args.get("patch") and not args.get("content"):
        args["content"] = args["patch"]

    # str_replace_editor is a dispatcher; its real verb is args["command"].
    # Resolve the mapped tool from that (create→Write, str_replace→Edit, …)
    # so the right secrets branch fires, not the top-level name alone.
    mapped = _COPILOT_TOOL_NAMES.get(name, name)
    if name == "str_replace_editor":
        mapped = _COPILOT_STR_REPLACE_VERBS.get(str(args.get("command", "")), "Edit")

    for src, dst in _COPILOT_ARG_ALIASES:
        if src in args and not args.get(dst):
            args[dst] = args[src]
    return mapped, args


# Copilot hook payload keys → the snake_case spelling handlers read.
_COPILOT_ALIASES = (
    ("sessionId", "session_id"),
    ("toolName", "tool_name"),
    ("toolArgs", "tool_input"),
    ("toolResult", "tool_response"),
    ("transcriptPath", "transcript_path"),
    ("workspaceRoot", "cwd"),
)


def _normalize_copilot(payload: dict[str, Any]) -> dict[str, Any]:
    # Real copilot payloads carry no event-name field at all — stdin is the
    # event's input object alone (observed live, v1.0.80). The adapter
    # registers each event with its name as argv[1] and the wrapper exports it
    # as AGENTIHOOKS_COPILOT_EVENT; payload spellings still win if a future
    # copilot starts sending one.
    raw_event = (
        payload.get("hookEventName")
        or payload.get("hook_event_name")
        or os.environ.get("AGENTIHOOKS_COPILOT_EVENT", "")
    )
    mapped = _COPILOT_EVENTS.get(raw_event)
    if mapped:
        payload["hook_event_name"] = mapped
        payload.setdefault("copilot_event_name", raw_event)

    # preToolUse carries a toolCalls ARRAY (batched parallel calls), each
    # with a stringified args field — not toolName/toolArgs (observed live,
    # v1.0.80). The first call folds into tool_name/tool_input; the rest ride
    # along for hook_manager to run through the same pipeline, since exit
    # codes can only deny the batch as a whole.
    calls = payload.get("toolCalls")
    if isinstance(calls, list) and calls:
        normalized = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            name, args = _copilot_tool_call(str(call.get("name", "")), call.get("args"))
            normalized.append({"tool_name": name, "tool_input": args})
        if normalized:
            if not payload.get("tool_name"):
                payload["tool_name"] = normalized[0]["tool_name"]
            if not payload.get("tool_input"):
                payload["tool_input"] = normalized[0]["tool_input"]
            if len(normalized) > 1:
                payload["copilot_extra_tool_calls"] = normalized[1:]

    # Not setdefault: a destination key that is PRESENT but empty ({} / "" /
    # None) would win over the real value and hand every guardrail blank
    # arguments while the tool call still executes. Fill whenever the
    # destination has no content of its own.
    for camel, snake in _COPILOT_ALIASES:
        value = payload.get(camel)
        if value:
            if not payload.get(snake):
                payload[snake] = value
        elif payload.get(snake):
            payload[camel] = payload[snake]

    # postToolUse sends singular toolName/toolArgs, args stringified and the
    # tool named in copilot's vocabulary — same translation as toolCalls so
    # pre/post pairs (retry breaker, tool memory) agree on the name.
    if payload.get("tool_name"):
        name, args = _copilot_tool_call(str(payload["tool_name"]), payload.get("tool_input"))
        payload["tool_name"] = name
        if args:
            payload["tool_input"] = args

    resp = payload.get("tool_response")
    if resp is not None:
        payload.setdefault("tool_output", resp)
        payload.setdefault("tool_result", resp)

    if payload.get("hook_event_name", "") in _TRANSCRIPT_EVENTS and not payload.get("transcript_path"):
        resolved = copilot_events_path(str(payload.get("session_id", "")))
        if resolved:
            payload["transcript_path"] = resolved
    return payload


# Codex's hook boundary already speaks Claude's vocabulary for the shell tool
# (verified live, 0.154.0: tool_name "Bash", tool_input {"command": "<string>"}),
# even though the model-facing tool is named `exec` in the rollout. The patch
# tool is the exception: it arrives as `apply_patch` with the whole patch body
# in `command`, reaching neither the Bash branch nor the Write/Edit one, so a
# secret written through a patch was never scanned. The shell aliases are
# insurance against a version that stops translating.
_CODEX_TOOL_NAMES = {
    "apply_patch": "Edit",
    "exec": "Bash",
    "shell": "Bash",
    "local_shell": "Bash",
    "unified_exec": "Bash",
    "read_file": "Read",
}

# `*** Add File: path` / `*** Update File: path` / `*** Delete File: path`
_CODEX_PATCH_TARGET = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)


def _codex_tool_call(name: str, args: Any) -> tuple[str, dict[str, Any]]:
    """Map one codex tool call onto the (tool_name, tool_input) handlers read."""
    if not isinstance(args, dict):
        args = {}
    mapped = _CODEX_TOOL_NAMES.get(name, name)

    # 0.147 sent {"command": ["ls", "-la"]}; a list reaches every guard as an
    # AttributeError, which hook_manager turns into "guard bypassed".
    if isinstance(args.get("command"), list):
        args["command"] = shlex.join(str(part) for part in args["command"])
    # code-mode `exec` carries a freeform program in `input`; the shell command
    # it runs is a literal substring, which is all the regex guards need.
    if not args.get("command") and isinstance(args.get("input"), str):
        args["command"] = args["input"]
        args.setdefault("content", args["input"])

    if name == "apply_patch":
        body = args.get("command") or args.get("patch") or ""
        # Both spellings: the secrets scan reads `content`, version_guard reads
        # `new_string`.
        args.setdefault("content", body)
        args.setdefault("new_string", body)
        target = _CODEX_PATCH_TARGET.search(body)
        if target and not args.get("file_path"):
            args["file_path"] = target.group(1).strip()
    return mapped, args


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if is_copilot():
        return _normalize_copilot(payload)
    if not is_codex():
        return payload

    if payload.get("tool_name"):
        name, args = _codex_tool_call(str(payload["tool_name"]), payload.get("tool_input"))
        payload["tool_name"] = name
        if args:
            payload["tool_input"] = args

    # Some handlers read the older ``tool_output`` / ``tool_result`` aliases.
    resp = payload.get("tool_response")
    if resp is not None:
        payload.setdefault("tool_output", resp)
        payload.setdefault("tool_result", resp)

    # Codex never sends ``transcript_path``; resolve its rollout from the
    # session id so transcript-driven features work the same on both targets.
    # Only on the events that actually read a transcript — resolving on every
    # PreToolUse/PostToolUse would charge a filesystem lookup to every tool
    # call in the turn for consumers that never look at the value.
    if payload.get("hook_event_name", "") in _TRANSCRIPT_EVENTS and not payload.get("transcript_path"):
        resolved = codex_rollout_path(str(payload.get("session_id", "")))
        if resolved:
            payload["transcript_path"] = resolved

    # Codex SessionEnd carries ``reason`` (currently only "other"); nothing to map.
    # Codex adds ``turn_id`` on turn-scoped events; handlers ignore unknown keys.
    return payload
