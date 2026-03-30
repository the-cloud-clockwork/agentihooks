#!/usr/bin/env python3
"""
Tool Memory - Cross-session error learning for Claude Code hooks.

Importable module called from hook_manager.py (not standalone).

Public API:
    inject_memory()            # PreToolUse: print past errors to stdout (Claude sees this)
    record_error(payload)      # PostToolUse: detect & save errors from tool_response
    scan_transcript(payload)   # Stop: scan transcript for MCP errors missed by PostToolUse

NOTE: Claude Code does NOT fire PostToolUse when MCP tools return errors.
The scan_transcript() function (registered on Stop hook) catches those missed errors
by scanning the full transcript at session end.

Environment:
    AGENTICORE_TOOL_MEMORY_PATH   - NDJSON file path (default: ~/.agenticore_tool_memory.ndjson)
    AGENTICORE_TOOL_MEMORY_MAX    - Max entries to keep (default: 100)
    AGENTICORE_TOOL_MEMORY_SHOW   - Max entries to show on inject (default: 15)

NDJSON record format:
    {"ts": "ISO8601", "tool": "tool_name", "error": "error text", "input": "input summary", "session": "session_id"}
"""

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MEMORY_PATH = Path(
    os.environ.get("AGENTICORE_TOOL_MEMORY_PATH", os.path.expanduser("~/.agenticore_tool_memory.ndjson"))
)
MAX_ENTRIES = int(os.environ.get("AGENTICORE_TOOL_MEMORY_MAX", "100"))
MAX_SHOW = int(os.environ.get("AGENTICORE_TOOL_MEMORY_SHOW", "15"))

# ---------------------------------------------------------------------------
# Error detection
# ---------------------------------------------------------------------------

ERROR_PATTERNS = [
    "error",
    "exception",
    "failed",
    "traceback",
    "denied",
    "timeout",
    "refused",
    "not found",
    "invalid",
    "unauthorized",
    "forbidden",
    "enoent",
    "syntax error",
    "unexpected token",
]

# Patterns that are false positives (common in successful responses)
FALSE_POSITIVE_PATTERNS = [
    "no errors",
    "no error",
    "without error",
    "error handling",
    "error_handling",
    "errorhandling",
    "not found any",
    "found no",
    "if error",
    "on error",
    "error log",
    "error_log",
    # Monitoring / diagnostic output (BTRFS, NVMe, system stats)
    "io_errs",
    "media_errors",
    "corruption_errs",
    "generation_errs",
    "flush_io_errs",
    "num_err_log_entries",
    "error_count",
    "errors finding",
    "finding 0 errors",
]


def _is_error(tool_result, strict=False):
    """Detect if tool_result contains an error. Returns (is_error, error_text).

    Args:
        tool_result: The tool response dict or string.
        strict: If True, only trust explicit flags (is_error, exitCode).
                Skip string pattern matching which produces false positives
                on MCP tool responses containing arbitrary text (e.g., Jira
                issue descriptions with words like "error", "not found").
    """
    if isinstance(tool_result, dict):
        # Explicit error flag
        if tool_result.get("is_error"):
            content = tool_result.get("content", tool_result.get("error", ""))
            if isinstance(content, list):
                # Handle content blocks format: [{"type": "text", "text": "..."}]
                texts = []
                for block in content:
                    if isinstance(block, dict) and block.get("text"):
                        texts.append(block["text"])
                content = " ".join(texts) if texts else str(content)
            return True, str(content)[:200]

        # Non-zero exit code (Bash)
        exit_code = tool_result.get("exitCode", tool_result.get("exit_code", 0))
        if exit_code and exit_code != 0:
            stderr = tool_result.get("stderr", "")
            return True, (stderr or str(tool_result.get("stdout", "")))[:200]

    # In strict mode, skip string pattern matching (MCP tools).
    # MCP responses contain arbitrary user content that triggers false positives.
    if strict:
        return False, ""

    # If Bash exited with code 0, trust the exit code over string matching.
    # Successful commands often contain words like "error" in their output
    # (e.g., btrfs stats: "write_io_errs 0", nvme: "media_errors 0").
    if isinstance(tool_result, dict):
        exit_code = tool_result.get("exitCode", tool_result.get("exit_code"))
        if exit_code is not None and exit_code == 0:
            return False, ""

    # String pattern matching (Bash tools only — structured output)
    result_str = str(tool_result).lower()

    # Check false positives first
    for fp in FALSE_POSITIVE_PATTERNS:
        if fp in result_str:
            return False, ""

    for pattern in ERROR_PATTERNS:
        if pattern in result_str:
            return True, str(tool_result)[:200]

    return False, ""


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _read_entries():
    """Read all NDJSON entries from memory file."""
    if not MEMORY_PATH.exists():
        return []
    entries = []
    try:
        with open(MEMORY_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception:  # NOSONAR — hooks must never crash the parent process
        return []
    return entries


def _append_entry(entry):
    """Append a single NDJSON entry, rotating if needed."""
    try:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Read existing entries to check count
        entries = _read_entries()
        entries.append(entry)

        # Rotate if exceeded max
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
            # Rewrite entire file
            with open(MEMORY_PATH, "w") as f:
                for e in entries:
                    f.write(json.dumps(e, separators=(",", ":")) + "\n")
        else:
            # Just append
            with open(MEMORY_PATH, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent failure - never break the agent


def _append_entries(new_entries):
    """Append multiple NDJSON entries, rotating if needed."""
    if not new_entries:
        return
    try:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        entries = _read_entries()
        entries.extend(new_entries)

        # Rotate if exceeded max
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]

        # Rewrite entire file
        with open(MEMORY_PATH, "w") as f:
            for e in entries:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass


# ---------------------------------------------------------------------------
# Input summary extraction
# ---------------------------------------------------------------------------


def _extract_input_summary(tool_input):
    """Extract a compact input summary from tool_input dict."""
    if isinstance(tool_input, dict):
        for key in ("command", "jql", "query", "pattern", "sql", "issue_key"):
            if key in tool_input:
                return str(tool_input[key])[:150]
        # First key-value pair
        for k, v in tool_input.items():
            return f"{k}={str(v)[:120]}"
    return str(tool_input)[:150] if tool_input else ""


# ---------------------------------------------------------------------------
# Transcript scanning
# ---------------------------------------------------------------------------


def _scan_transcript_for_errors(transcript_path, session_id=""):
    """Scan transcript JSONL for tool errors. Returns list of memory entries."""
    from datetime import datetime, timezone

    if not transcript_path:
        return []
    path = Path(transcript_path)
    if not path.exists():
        return []

    new_entries = []
    try:
        with open(path, "r") as f:
            content = f.read()

        # Transcript can be JSONL (one JSON per line) or a JSON array
        entries = []
        content_stripped = content.strip()
        if content_stripped.startswith("["):
            # JSON array format
            try:
                entries = json.loads(content_stripped)
            except json.JSONDecodeError:
                return []
        else:
            # JSONL format
            for line in content_stripped.split("\n"):
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        # Transcript format:
        #   assistant entries have content[].type="tool_use" with id, name, input
        #   user entries have content[].type="tool_result" with tool_use_id, is_error, content
        # We correlate tool_use -> tool_result via tool_use_id

        tool_uses = {}  # tool_use_id -> {tool_name, tool_input}

        for entry in entries:
            entry_type = entry.get("type", "")

            if entry_type == "assistant":
                message = entry.get("message", {})
                content_blocks = message.get("content", [])
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        use_id = block.get("id", "")
                        if use_id:
                            tool_uses[use_id] = {
                                "tool_name": block.get("name", "unknown"),
                                "tool_input": block.get("input", {}),
                            }

            elif entry_type == "user":
                # tool_result blocks are nested inside user messages
                message = entry.get("message", {})
                content_blocks = message.get("content", []) if isinstance(message, dict) else []
                if not isinstance(content_blocks, list):
                    continue

                for block in content_blocks:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue

                    tool_use_id = block.get("tool_use_id", "")
                    is_err = block.get("is_error", False)
                    result_content = block.get("content", "")

                    # Extract text from content
                    if isinstance(result_content, list):
                        texts = []
                        for sub in result_content:
                            if isinstance(sub, dict) and sub.get("text"):
                                texts.append(sub["text"])
                        result_text = " ".join(texts)
                    else:
                        result_text = str(result_content)

                    # Check if this is an error
                    # For transcript scanning, ONLY trust explicit is_error flag
                    # String pattern matching produces too many false positives
                    # on successful JSON responses that contain words like "error"
                    detected = False
                    error_text = ""

                    if is_err:
                        detected = True
                        error_text = result_text[:200]

                    if detected and error_text:
                        tool_info = tool_uses.get(tool_use_id, {})
                        tool_name = tool_info.get("tool_name", "unknown")
                        tool_input = tool_info.get("tool_input", {})

                        ts = entry.get("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

                        new_entries.append(
                            {
                                "ts": ts[:19] + "Z" if len(ts) > 19 else ts,
                                "tool": tool_name,
                                "error": error_text.strip()[:200],
                                "input": _extract_input_summary(tool_input),
                                "session": session_id,
                            }
                        )

    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass

    return new_entries


# ---------------------------------------------------------------------------
# Public API (called from hook_manager.py)
# ---------------------------------------------------------------------------


def _seen_tools_path(session_id):
    """Return path to session-scoped file tracking which tools have been shown memory."""
    return Path(f"/tmp/.tool_memory_seen_{session_id}")


def _is_tool_seen(session_id, tool_name):
    """Check if memory was already injected for this tool in this session."""
    if not session_id:
        return False
    try:
        path = _seen_tools_path(session_id)
        if not path.exists():
            return False
        seen = path.read_text().splitlines()
        return tool_name in seen
    except Exception:
        return False


def _mark_tool_seen(session_id, tool_name):
    """Mark that memory was injected for this tool in this session."""
    if not session_id:
        return
    try:
        path = _seen_tools_path(session_id)
        with open(path, "a") as f:
            f.write(tool_name + "\n")
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass


def inject_memory(tool_name="", session_id=""):
    """PreToolUse: Print past errors to stdout (Claude sees this).

    Only injects ONCE per tool per session. If memory was already shown
    for this tool_name in this session_id, silently skips to avoid noise.

    Args:
        tool_name: Current tool being invoked (for dedup tracking).
        session_id: Current session ID (for scoping the dedup).
    """
    # Skip if already injected for this tool in this session
    if tool_name and session_id and _is_tool_seen(session_id, tool_name):
        return

    entries = _read_entries()
    if not entries:
        return  # Exit silently - no memory yet

    # Show last N entries
    to_show = entries[-MAX_SHOW:]

    lines = []
    for e in to_show:
        ts = e.get("ts", "?")
        # Trim ISO timestamp to readable format
        if len(ts) >= 16:
            ts = ts[:16].replace("T", " ")
        tool = e.get("tool", "?")
        error = e.get("error", "?")
        inp = e.get("input", "")
        line = f"[{ts}] {tool} -- {error}"
        if inp:
            line += f" (input: {inp})"
        lines.append(line)

    # Use inject_banner from common.py — same pattern as all other hooks
    # This prints to STDOUT (Claude sees it) AND logs to hooks.log for debugging
    from hooks.common import inject_banner

    inject_banner("TOOL MEMORY: Lessons from past sessions", "\n".join(lines))

    # Mark as seen so we don't inject again for this tool in this session
    if tool_name and session_id:
        _mark_tool_seen(session_id, tool_name)


def record_error(payload):
    """PostToolUse: Detect errors and save to memory file.

    Args:
        payload: Hook payload dict with tool_name, tool_input, tool_response/tool_result, session_id.
    """
    from datetime import datetime, timezone

    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {})
    # Claude Code uses "tool_response" in PostToolUse payload (not "tool_result")
    tool_result = payload.get("tool_response") or payload.get("tool_result")

    # If no result in payload, skip (scan_transcript will catch it at session end)
    if tool_result is None:
        return

    # Detect error
    # For MCP tools, ONLY trust explicit flags (is_error, exitCode).
    # String pattern matching produces false positives on MCP responses
    # because Jira descriptions contain words like "error", "not found", etc.
    is_mcp = tool_name.startswith("mcp__")
    detected, error_text = _is_error(tool_result, strict=is_mcp)
    if not detected:
        return  # No error - exit silently

    # Build NDJSON record
    session_id = payload.get("session_id", "")
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": tool_name,
        "error": error_text.strip()[:200],
        "input": _extract_input_summary(tool_input),
        "session": session_id,
    }

    _append_entry(entry)


def scan_transcript(payload):
    """Stop: Scan transcript for tool errors missed by PostToolUse.

    Claude Code does NOT fire PostToolUse when MCP tools return errors.
    This function runs at session end (Stop hook) and scans the full
    transcript to catch those missed errors.

    Args:
        payload: Hook payload dict with transcript_path and session_id.
    """
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")

    if not transcript_path:
        return

    # Scan transcript for errors
    new_entries = _scan_transcript_for_errors(transcript_path, session_id)

    if not new_entries:
        return

    # Deduplicate against existing entries (by tool+error combo)
    existing = _read_entries()
    existing_keys = set()
    for e in existing:
        key = (e.get("tool", ""), e.get("error", "")[:100])
        existing_keys.add(key)

    unique_entries = []
    for e in new_entries:
        key = (e.get("tool", ""), e.get("error", "")[:100])
        if key not in existing_keys:
            unique_entries.append(e)
            existing_keys.add(key)

    if unique_entries:
        _append_entries(unique_entries)
