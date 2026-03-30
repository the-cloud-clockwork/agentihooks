#!/usr/bin/env python3
"""
Hook Manager - Centralized entry point for all Claude Code hooks.

All hooks in settings.json point to this file.
The event name is passed via stdin JSON payload (hook_event_name field).

Supported events:
    - PreToolUse
    - PostToolUse
    - UserPromptSubmit
    - Stop
    - SubagentStop
    - SessionStart
    - SessionEnd
    - Notification
    - PreCompact
    - PermissionRequest

PERFORMANCE NOTE: Heavy imports (email, transcript) are lazy-loaded only when needed
to reduce startup time for frequent events like PreToolUse/PostToolUse.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

from hooks.observability import otel


# Add parent directory to path for direct execution
sys.path.insert(0, str(Path(__file__).parent.parent))

# Only import lightweight common module at startup
from hooks.common import log


class BlockAction(Exception):
    """Raised by a hook handler to block the current Claude Code action (exit 2)."""


# Heavy imports are lazy-loaded in functions that need them:
# - hooks.integrations.email.send_email -> only in notify_on_error()
# - hooks.observability.transcript.log_new_entries -> only in handlers that need it


# =============================================================================
# TRANSCRIPT METRICS PARSER
# =============================================================================


def log_claude_max_output_tokens():
    """Inject the output token limit into Claude's context window.

    This makes Claude aware of response size constraints so it can proactively
    keep responses under the limit to avoid 'exceeded output token maximum' errors.
    """
    try:
        max_tokens = os.getenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS")

        if max_tokens:
            # Use inject_banner to output to STDOUT (Claude sees this!)
            from hooks.common import inject_banner

            content = f"""Your responses MUST stay under {max_tokens} tokens to avoid errors.

RULES:
• Be concise - no verbose explanations
• Skip unnecessary comments in code
• Break large outputs into smaller chunks
• Prefer summaries over exhaustive details"""

            inject_banner(f"⚠️ OUTPUT TOKEN LIMIT: {max_tokens} tokens", content)
    except Exception as e:
        log("Failed to inject output token limit", {"error": str(e)})


def parse_transcript_metrics(transcript_path: str) -> dict:
    """
    Parse transcript JSONL to extract session metrics.

    Returns:
        dict with num_turns, duration_ms, last_response
    """
    from datetime import datetime  # Lazy import

    metrics = {
        "num_turns": 0,
        "duration_ms": None,
        "last_response": None,
    }

    try:
        path = Path(transcript_path)
        if not path.exists():
            return metrics

        first_timestamp = None
        last_timestamp = None
        user_count = 0
        last_assistant_text = None

        with open(path, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    entry_type = entry.get("type")

                    # Track timestamps for duration
                    timestamp_str = entry.get("timestamp")
                    if timestamp_str:
                        try:
                            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                            if first_timestamp is None:
                                first_timestamp = ts
                            last_timestamp = ts
                        except (ValueError, TypeError):
                            pass

                    # Count user messages (turns)
                    if entry_type == "user":
                        user_count += 1

                    # Track last assistant response
                    elif entry_type == "assistant":
                        message = entry.get("message", {})
                        content = message.get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                last_assistant_text = block.get("text")

                except json.JSONDecodeError:
                    continue

        metrics["num_turns"] = user_count
        metrics["last_response"] = last_assistant_text

        # Calculate duration
        if first_timestamp and last_timestamp:
            duration = last_timestamp - first_timestamp
            metrics["duration_ms"] = int(duration.total_seconds() * 1000)

    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Return partial metrics on error

    return metrics


# =============================================================================
# ERROR NOTIFICATION
# =============================================================================

# Error patterns to detect (case-insensitive)
ERROR_PATTERNS = [
    "api error",
    "error:",
    "exception",
    "failed",
    "traceback",
    "rate limit",
    "timeout",
    "connection refused",
]


def notify_on_error(transcript_path: str) -> None:
    """
    Check transcript for errors and send email notification if found.

    Scans the entire transcript for error patterns and sends notification
    to recipients specified in email.json (if found in working directory).

    Args:
        transcript_path: Path to the JSONL transcript file.
    """
    if not transcript_path:
        return

    try:
        path = Path(transcript_path)
        if not path.exists():
            return

        # Read entire transcript
        content = path.read_text(encoding="utf-8")
        content_lower = content.lower()

        # Check if any error pattern exists
        detected_patterns = [p for p in ERROR_PATTERNS if p in content_lower]
        if not detected_patterns:
            return  # No errors found

        # Extract error context from transcript lines
        error_lines = []
        for line in content.split("\n"):
            line_lower = line.lower()
            if any(p in line_lower for p in ERROR_PATTERNS):
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if isinstance(msg, dict):
                        for block in msg.get("content", []):
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                if any(p in text.lower() for p in ERROR_PATTERNS):
                                    error_lines.append(text[:500])
                    elif isinstance(msg, str):
                        error_lines.append(msg[:500])
                except json.JSONDecodeError:
                    error_lines.append(line[:500])

        # Build error report
        error_context = "\n\n---\n\n".join(error_lines[:5])  # Max 5 errors
        patterns_found = ", ".join(detected_patterns)

        # Lazy import mailer module (only needed here)
        from hooks.integrations.mailer import (
            scan_for_config_files,
            send_error_notification,
        )

        # Scan for email.json configuration file and template
        email_config, template = scan_for_config_files()

        if not email_config:
            log("Email notification skipped: email.json not found in working directory")
            return

        # Get agent name from environment or use default
        agent_name = os.getenv("AGENT_NAME", "Agent")

        # Build notification content
        notification_content = f"""# Error Detected in Agent Session

**Agent:** {agent_name}
**Transcript:** `{transcript_path}`
**Patterns Detected:** {patterns_found}

## Error Details

{error_context if error_context else "Error detected in transcript (see logs for full details)"}

---
*Automated notification from {agent_name} hook system.*
"""

        # Send notification using error_recipients category with template support
        result = send_error_notification(
            config=email_config,
            template=template,
            content=notification_content,
            subject=f"{agent_name} - Error Detected",
            # Extra template variables
            error_type="transcript_scan",
            patterns=patterns_found,
            agent_name=agent_name,
        )

        if result.success:
            log(
                "Error notification sent",
                {
                    "category": "error_recipients",
                    "recipients_count": result.recipients_count,
                    "patterns": detected_patterns,
                },
            )
        else:
            log("Failed to send error notification", {"error": result.error})

    except Exception as e:
        # Silent failure - never break the agent
        log("notify_on_error failed", {"error": str(e)})


# =============================================================================
# EVENT HANDLERS
# =============================================================================


def on_session_start(payload: dict) -> None:
    """Handle SessionStart event."""
    session_id = payload.get("session_id", "")
    log("Session started", {"session_id": session_id})
    otel.emit_event("agentihooks.session.started", {
        "session.id": session_id,
        "agent.name": os.environ.get("AGENT_NAME", "unknown"),
    })

    # Emit a trace span for session start (visible in Langfuse)
    tracer = otel.get_tracer()
    if tracer:
        with tracer.start_as_current_span("agentihooks.session.start", attributes={
            "session.id": session_id,
            "agent.name": os.environ.get("AGENT_NAME", "unknown"),
        }):
            pass  # span auto-ends on context exit → exported immediately

    # Log output token limit so Claude is aware of response size constraints
    log_claude_max_output_tokens()

    from hooks.config import MCP_HYGIENE_ENABLED

    if MCP_HYGIENE_ENABLED:
        from hooks.common import inject_context

        inject_context(
            "TOKEN CONTROL ACTIVE: Multiple MCP servers loaded. "
            "Disable unused servers via /mcp to reduce per-turn token overhead. "
            "Model guidance: use Sonnet 4.6 for implementation; reserve Opus 4.6 for plan mode (Shift+Tab)."
        )

    # Thinking/effort policy guidance
    try:
        from hooks.config import EFFORT_POLICY_ENABLED, DEFAULT_EFFORT, THINKING_BUDGET_TOKENS

        if EFFORT_POLICY_ENABLED:
            from hooks.context.thinking_policy import get_thinking_guidance
            from hooks.common import inject_context as _inject

            guidance = get_thinking_guidance(DEFAULT_EFFORT, THINKING_BUDGET_TOKENS)
            if guidance:
                _inject(guidance)
    except Exception as e:
        log("thinking_policy injection failed", {"error": str(e)})

    # MCP surface area warning
    try:
        from hooks.config import MCP_TOOL_WARN_THRESHOLD, MCP_SCHEMA_AVG_TOKENS
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            "mcp_reporter",
            str(Path(__file__).resolve().parent.parent / "scripts" / "mcp_reporter.py"),
        )
        if _spec and _spec.loader:
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)

            servers = _mod.load_all_mcp_configs()
            if servers:
                warning = _mod.generate_warning(servers, MCP_TOOL_WARN_THRESHOLD, MCP_SCHEMA_AVG_TOKENS)
                if warning:
                    from hooks.common import inject_context as _inject2

                    _inject2(warning)
    except Exception as e:
        log("mcp_reporter warning failed", {"error": str(e)})


def on_session_end(payload: dict) -> None:
    """Handle SessionEnd event."""
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")

    # Parse transcript to get metrics (Claude Code doesn't include these in hook payload)
    metrics = parse_transcript_metrics(transcript_path) if transcript_path else {}

    log(
        "Session ended",
        {
            "session_id": session_id,
            "num_turns": metrics.get("num_turns"),
            "duration_ms": metrics.get("duration_ms"),
        },
    )

    # Log transcript entries to hooks.log (for debugging)
    if session_id and transcript_path:
        from hooks.observability.transcript import log_new_entries

        log_new_entries(session_id, transcript_path)

    # Clear file read cache for this session
    from hooks.config import FILE_READ_CACHE_ENABLED

    if FILE_READ_CACHE_ENABLED:
        try:
            from hooks.context.file_read_cache import clear_session_cache

            clear_session_cache(session_id)
        except Exception:
            pass

    # Clear retry breaker state for this session
    from hooks.config import RETRY_BREAKER_ENABLED

    if RETRY_BREAKER_ENABLED:
        try:
            from hooks.context.retry_breaker import clear_session_state

            clear_session_state(session_id)
        except Exception:
            pass

    # Clear context audit state for this session
    from hooks.config import CONTEXT_AUDIT_ENABLED

    if CONTEXT_AUDIT_ENABLED:
        try:
            from hooks.observability.context_audit import clear_session_audit

            clear_session_audit(session_id)
        except Exception:
            pass


def on_user_prompt_submit(payload: dict) -> None:
    """Handle UserPromptSubmit event."""
    from hooks.config import SECRETS_MODE

    session_id = payload.get("session_id", "")
    log("User prompt submitted", {"session_id": session_id})

    if SECRETS_MODE == "off":
        log("Secrets scanning skipped (mode=off)")
        return

    prompt = payload.get("prompt", "")
    if prompt:
        from hooks.common import inject_context
        from hooks.secrets import scan

        hits = scan(prompt, mode=SECRETS_MODE)
        if hits:
            names = ", ".join(hits)
            inject_context(
                f"WARNING: Possible secret(s) detected in prompt ({names}). "
                "Do not process, echo, or store credential values. "
                "Ask the user to use environment variables instead."
            )


def on_pre_tool_use(payload: dict) -> None:
    """Handle PreToolUse event."""
    from hooks.config import SECRETS_MODE

    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {})

    if SECRETS_MODE == "off":
        log(f"Pre tool use: {tool_name} (secrets scanning skipped, mode=off)", {"tool": tool_name})
    else:
        from hooks.secrets import redact, scan

        # Log command details for Bash
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            safe_command = redact(command, mode=SECRETS_MODE)[:500]
            log(f"Pre tool use: {tool_name}", {"tool": tool_name, "command": safe_command})
            hits = scan(command, mode=SECRETS_MODE)
            if hits:
                names = ", ".join(hits)
                if SECRETS_MODE == "warn":
                    from hooks.common import inject_context

                    inject_context(
                        f"WARNING: Possible secret(s) detected in Bash command ({names}). "
                        "Never pass credentials as inline command arguments. "
                        "Use environment variables instead."
                    )
                else:
                    otel.emit_event("agentihooks.guardrail.secret_detected", {
                        "session.id": payload.get("session_id", ""),
                        "tool_name": tool_name,
                        "secret_types": names,
                        "action": "block",
                    })
                    _tracer = otel.get_tracer()
                    if _tracer:
                        with _tracer.start_as_current_span("agentihooks.guardrail.secret_blocked", attributes={
                            "session.id": payload.get("session_id", ""),
                            "tool_name": tool_name,
                            "secret_types": names,
                        }):
                            pass
                    raise BlockAction(
                        f"BLOCKED: Secret(s) detected in Bash command ({names}). "
                        "Never pass credentials as inline command arguments. "
                        "Use environment variables instead."
                    )
        elif tool_name in ("Write", "Edit"):
            content = tool_input.get("content", "") or tool_input.get("new_string", "")
            log(f"Pre tool use: {tool_name}", {"tool": tool_name})
            hits = scan(content, mode=SECRETS_MODE)
            if hits:
                names = ", ".join(hits)
                if SECRETS_MODE == "warn":
                    from hooks.common import inject_context

                    inject_context(
                        f"WARNING: Possible secret(s) detected in {tool_name} content ({names}). "
                        "Never write credential values to files. "
                        "Use environment variables instead."
                    )
                else:
                    otel.emit_event("agentihooks.guardrail.secret_detected", {
                        "session.id": payload.get("session_id", ""),
                        "tool_name": tool_name,
                        "secret_types": names,
                        "action": "block",
                    })
                    raise BlockAction(
                        f"BLOCKED: Secret(s) detected in {tool_name} content ({names}). "
                        "Never write credential values to files. "
                        "Use environment variables instead."
                    )
        else:
            log(f"Pre tool use: {tool_name}", {"tool": tool_name})

    # Log transcript entries to hooks.log (for debugging)
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")

    if session_id and transcript_path:
        from hooks.observability.transcript import log_new_entries

        log_new_entries(session_id, transcript_path)

    # File read deduplication
    if tool_name == "Read":
        try:
            from hooks.config import FILE_READ_CACHE_ENABLED

            if FILE_READ_CACHE_ENABLED:
                from hooks.context.file_read_cache import check_and_block_redundant_read

                check_and_block_redundant_read(payload)  # raises BlockAction if blocked
        except BlockAction:
            otel.emit_event("agentihooks.guardrail.read_deduplicated", {
                "session.id": payload.get("session_id", ""),
                "file_path": payload.get("tool_input", {}).get("file_path", ""),
            })
            raise
        except Exception as e:
            log("file_read_cache check failed", {"error": str(e)})

    # Inject tool memory (past errors) into Claude's context
    # Only injects once per tool per session to avoid noise
    from hooks.tool_memory import inject_memory

    inject_memory(tool_name=tool_name, session_id=session_id)

    # Retry circuit breaker — hard block on excessive retries
    from hooks.config import RETRY_BREAKER_ENABLED

    if RETRY_BREAKER_ENABLED:
        try:
            from hooks.context.retry_breaker import check_hard_block

            check_hard_block(payload)  # raises BlockAction if count >= hard max
        except BlockAction:
            otel.emit_event("agentihooks.guardrail.retry_blocked", {
                "session.id": payload.get("session_id", ""),
                "tool_name": payload.get("tool_name", "unknown"),
            })
            _tracer = otel.get_tracer()
            if _tracer:
                with _tracer.start_as_current_span("agentihooks.guardrail.retry_blocked", attributes={
                    "session.id": payload.get("session_id", ""),
                    "tool_name": payload.get("tool_name", "unknown"),
                }):
                    pass
            raise
        except Exception as e:
            log("retry_breaker pre-tool failed", {"error": str(e)})


def on_post_tool_use(payload: dict) -> None:
    """Handle PostToolUse event."""
    tool_name = payload.get("tool_name", "unknown")
    log(f"Post tool use: {tool_name}", {"tool": tool_name})

    # Log transcript entries to hooks.log (for debugging)
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    if session_id and transcript_path:
        from hooks.observability.transcript import log_new_entries

        log_new_entries(session_id, transcript_path)

    # Bash output filtering
    if tool_name == "Bash":
        try:
            from hooks.config import BASH_FILTER_ENABLED

            if BASH_FILTER_ENABLED:
                from hooks.context.bash_output_filter import filter_bash_output

                filtered = filter_bash_output(tool_name, payload.get("tool_input", {}), payload.get("tool_output", ""))
                if filtered is not None:
                    import json as _json

                    otel.emit_event("agentihooks.context.output_filtered", {
                        "session.id": payload.get("session_id", ""),
                        "tool_name": tool_name,
                    })
                    print(_json.dumps({"additionalContext": filtered}))
        except Exception as e:
            log("bash_output_filter failed", {"error": str(e)})

    # Mark file as read in cache
    if tool_name == "Read":
        try:
            from hooks.config import FILE_READ_CACHE_ENABLED

            if FILE_READ_CACHE_ENABLED:
                from hooks.context.file_read_cache import mark_file_read

                file_path = payload.get("tool_input", {}).get("file_path", "")
                if file_path and payload.get("session_id"):
                    mark_file_read(payload["session_id"], file_path)
        except Exception as e:
            log("file_read_cache mark failed", {"error": str(e)})

    # Record tool errors to memory file
    from hooks.tool_memory import record_error

    record_error(payload)

    # Emit OTEL event if error was recorded
    is_error = payload.get("is_error", False)
    exit_code = payload.get("tool_input", {}).get("exitCode")
    if is_error or (exit_code and str(exit_code) != "0"):
        otel.emit_event("agentihooks.error.recorded", {
            "session.id": payload.get("session_id", ""),
            "tool_name": payload.get("tool_name", "unknown"),
        })

    # Retry circuit breaker — track failures and inject research instructions
    from hooks.config import RETRY_BREAKER_ENABLED

    if RETRY_BREAKER_ENABLED:
        try:
            from hooks.context.retry_breaker import on_post_tool_result

            on_post_tool_result(payload)
        except Exception as e:
            log("retry_breaker post-tool failed", {"error": str(e)})

    # Context audit — record tool output size
    try:
        from hooks.config import CONTEXT_AUDIT_ENABLED

        if CONTEXT_AUDIT_ENABLED:
            from hooks.observability.context_audit import record_tool_usage

            output = payload.get("tool_output", "")
            output_size = len(output.encode("utf-8")) if isinstance(output, str) else len(str(output))
            if payload.get("session_id") and output_size > 0:
                record_tool_usage(payload["session_id"], tool_name, output_size)
    except Exception as e:
        log("context_audit record failed", {"error": str(e)})

    # Thinking/effort policy — check subagent effort alignment
    try:
        from hooks.config import EFFORT_POLICY_ENABLED, DEFAULT_EFFORT

        if EFFORT_POLICY_ENABLED and tool_name == "Agent":
            from hooks.context.thinking_policy import check_subagent_effort
            from hooks.common import inject_context as _inject_effort

            warning = check_subagent_effort(payload.get("tool_input", {}), DEFAULT_EFFORT)
            if warning:
                _inject_effort(warning)
    except Exception as e:
        log("thinking_policy check failed", {"error": str(e)})


def on_stop(payload: dict) -> None:
    """Handle Stop event."""
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")

    # Parse transcript to get metrics (Claude Code doesn't include these in hook payload)
    metrics = parse_transcript_metrics(transcript_path) if transcript_path else {}

    log(
        "Claude stopped",
        {
            "session_id": session_id,
            "num_turns": metrics.get("num_turns"),
            "duration_ms": metrics.get("duration_ms"),
        },
    )
    otel.emit_event("agentihooks.session.ended", {
        "session.id": session_id,
        "num_turns": str(metrics.get("num_turns", 0)),
        "duration_ms": str(metrics.get("duration_ms", 0)),
    })

    # Emit a trace span for session end (visible in Langfuse)
    tracer = otel.get_tracer()
    if tracer:
        with tracer.start_as_current_span("agentihooks.session.stop", attributes={
            "session.id": session_id,
            "num_turns": metrics.get("num_turns", 0),
            "duration_ms": metrics.get("duration_ms", 0),
        }):
            pass

    # Check for errors and notify
    notify_on_error(transcript_path)

    # Scan transcript for MCP errors missed by PostToolUse
    from hooks.tool_memory import scan_transcript

    scan_transcript(payload)

    # Log transcript entries to hooks.log (for debugging)
    if session_id and transcript_path:
        from hooks.observability.transcript import log_new_entries

        log_new_entries(session_id, transcript_path)

    # Auto-save session memory
    try:
        from hooks.config import MEMORY_AUTO_SAVE

        if MEMORY_AUTO_SAVE and session_id and transcript_path:
            from hooks.memory.auto_save import auto_save_session

            auto_save_session(session_id, transcript_path)
    except Exception as e:
        log("Memory auto-save failed", {"error": str(e)})

    # Context audit — emit report if fill_pct exceeds threshold
    try:
        from hooks.config import CONTEXT_AUDIT_ENABLED, CONTEXT_AUDIT_THRESHOLD_PCT

        if CONTEXT_AUDIT_ENABLED and session_id:
            from hooks.observability.context_audit import get_audit_summary, format_audit_report
            from hooks.observability.token_monitor import get_context_fill_pct

            fill_pct = get_context_fill_pct(payload)
            if fill_pct is not None and fill_pct >= CONTEXT_AUDIT_THRESHOLD_PCT:
                summary = get_audit_summary(session_id)
                if summary:
                    report = format_audit_report(summary, fill_pct)
                    if report:
                        log("Context audit report", {"fill_pct": fill_pct, "report": report})
    except Exception as e:
        log("context_audit report failed", {"error": str(e)})


def on_subagent_stop(payload: dict) -> None:
    """Handle SubagentStop event."""
    session_id = payload.get("session_id", "")
    log("Subagent stopped", {"session_id": session_id})

    # Log transcript entries to hooks.log (for debugging)
    transcript_path = payload.get("transcript_path", "")
    if session_id and transcript_path:
        from hooks.observability.transcript import log_new_entries

        log_new_entries(session_id, transcript_path)


def on_status_line(payload: dict) -> None:
    """Handle StatusLine event — emit terminal status bar text."""
    try:
        from hooks.config import TOKEN_MONITOR_ENABLED

        if not TOKEN_MONITOR_ENABLED:
            return

        from hooks.observability.token_monitor import (
            get_context_fill_pct,
            should_warn_context,
            update_context_metrics,
        )

        status_line = update_context_metrics(payload)
        print(status_line, flush=True)

        session_id = payload.get("session_id", "")
        fill_pct = get_context_fill_pct(payload)
        if fill_pct is not None and session_id:
            warn, level = should_warn_context(fill_pct, session_id)
            if warn:
                from hooks.common import inject_banner
                from hooks.config import TOKEN_CRITICAL_PCT, TOKEN_WARN_PCT

                if level == "critical":
                    inject_banner(
                        f"🚨 CONTEXT CRITICAL ({fill_pct:.0f}% used)",
                        f"Context window is {fill_pct:.0f}% full (threshold: {TOKEN_CRITICAL_PCT}%). "
                        "Compact context immediately with /compact or start a new session. "
                        "Avoid large file reads; prefer targeted searches.",
                    )
                else:
                    inject_banner(
                        f"⚠️ CONTEXT WARNING ({fill_pct:.0f}% used)",
                        f"Context window is {fill_pct:.0f}% full (threshold: {TOKEN_WARN_PCT}%). "
                        "Consider compacting context with /compact before it fills further.",
                    )
    except Exception as e:
        log("on_status_line failed", {"error": str(e)})


def on_notification(payload: dict) -> None:
    """Handle Notification event."""
    log("Notification", payload)


def on_pre_compact(payload: dict) -> None:
    """Handle PreCompact event."""
    session_id = payload.get("session_id", "")
    log("Pre compact", {"session_id": session_id})


def on_permission_request(payload: dict) -> None:
    """Handle PermissionRequest event."""
    tool_name = payload.get("tool_name", "unknown")
    log(f"Permission requested: {tool_name}", {"tool": tool_name})


# =============================================================================
# EVENT ROUTER
# =============================================================================

EVENT_HANDLERS = {
    "SessionStart": on_session_start,
    "SessionEnd": on_session_end,
    "UserPromptSubmit": on_user_prompt_submit,
    "PreToolUse": on_pre_tool_use,
    "PostToolUse": on_post_tool_use,
    "Stop": on_stop,
    "SubagentStop": on_subagent_stop,
    "Notification": on_notification,
    "PreCompact": on_pre_compact,
    "PermissionRequest": on_permission_request,
}


def main() -> None:
    """Main entry point - routes events to handlers."""
    try:
        # Read payload from stdin
        payload: dict[str, Any] = json.load(sys.stdin)

        # Get event name from payload
        event_name = payload.get("hook_event_name", "Unknown")

        # Route to handler
        handler = EVENT_HANDLERS.get(event_name)
        if handler:
            handler(payload)
        else:
            log(f"Unknown event: {event_name}", payload)

    except BlockAction as e:
        print(str(e), file=sys.stderr, flush=True)  # Claude Code reads stderr for hook messages
        sys.exit(2)  # blocks the action
    except json.JSONDecodeError:
        log("Failed to parse JSON payload")
    except Exception as e:
        log(f"Hook manager error: {str(e)}")


if __name__ == "__main__":
    main()
