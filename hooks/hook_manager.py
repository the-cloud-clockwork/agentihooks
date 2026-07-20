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
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hooks.observability import otel

# Add parent directory to path for direct execution
sys.path.insert(0, str(Path(__file__).parent.parent))

# Only import lightweight common module at startup
from hooks.common import log


class BlockAction(Exception):
    """Raised by a hook handler to block the current Claude Code action (exit 2)."""


_OTEL_SECRET_DETECTED = "agentihooks.guardrail.secret_detected"
_UNSET = "<unset>"


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
    otel.emit_event(
        "agentihooks.session.started",
        {
            "session.id": session_id,
            "agent.name": os.environ.get("AGENT_NAME", "unknown"),
        },
    )

    # Emit a trace span for session start (visible in Langfuse)
    tracer = otel.get_tracer()
    if tracer:
        with tracer.start_as_current_span(
            "agentihooks.session.start",
            attributes={
                "session.id": session_id,
                "agent.name": os.environ.get("AGENT_NAME", "unknown"),
            },
        ):
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
        from hooks.config import (
            DEFAULT_EFFORT,
            EFFORT_POLICY_ENABLED,
            THINKING_BUDGET_TOKENS,
        )

        if EFFORT_POLICY_ENABLED:
            from hooks.common import inject_context as _inject
            from hooks.context.thinking_policy import get_thinking_guidance

            guidance = get_thinking_guidance(DEFAULT_EFFORT, THINKING_BUDGET_TOKENS)
            if guidance:
                _inject(guidance)
    except Exception as e:
        log("thinking_policy injection failed", {"error": str(e)})

    # --- Auto dev-switch: if on main/master, switch to dev (create if missing) ---
    try:
        from hooks.config import AUTO_DEV_SWITCH_ENABLED

        if AUTO_DEV_SWITCH_ENABLED:
            from hooks.context.auto_dev_switch import (
                inject_on_session_start as _auto_dev,
            )

            _auto_dev(payload.get("cwd", ""))
    except Exception as e:
        log("auto_dev_switch session_start failed", {"error": str(e)})

    # --- CI Manifesto: inject doctrine at session start ---
    try:
        from hooks.config import CI_MANIFESTO_ENABLED

        if CI_MANIFESTO_ENABLED:
            from hooks.context.ci_manifesto import (
                inject_on_session_start as inject_ci_manifesto,
            )

            inject_ci_manifesto()
    except Exception as e:
        log("ci_manifesto session_start failed", {"error": str(e)})

    # --- Broadcast: register session + deliver pending ---
    # Brain adapter FIRST — publishes brain content to broadcast.json
    try:
        from hooks.config import BRAIN_ENABLED

        if BRAIN_ENABLED:
            from hooks.context.brain_adapter import inject_on_session_start

            inject_on_session_start()
    except Exception as e:
        log("brain_adapter session_start failed", {"error": str(e)})

    # Broadcast SECOND — reads broadcast.json (now includes brain content) and injects
    from hooks.config import BROADCAST_ENABLED

    if BROADCAST_ENABLED:
        try:
            from hooks.context.broadcast import (
                check_and_inject_broadcasts,
                register_session,
            )

            register_session(
                session_id,
                pid=os.getppid(),
                cwd=payload.get("cwd", ""),
                model=payload.get("model", ""),
            )
            check_and_inject_broadcasts(session_id)
        except Exception as e:
            log("broadcast session_start failed", {"error": str(e)})

    # Voice output — cleanup stale flags from dead sessions
    try:
        from hooks.context.voice_output import cleanup_stale_flags

        cleaned = cleanup_stale_flags()
        if cleaned:
            log("voice_output: cleaned stale flags", {"count": cleaned})
    except Exception:
        pass

    # MCP surface area warning
    try:
        import importlib.util

        from hooks.config import MCP_SCHEMA_AVG_TOKENS, MCP_TOOL_WARN_THRESHOLD

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

    # Clear brain adapter counter for this session
    try:
        from hooks.context.brain_adapter import clear_session_state as _clear_brain

        _clear_brain(session_id)
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

    # Clear all signals at session end (per-turn + session-scoped)
    try:
        from hooks.context.branch_guard import clear_branch_signal, clear_pr_signal
        from hooks.context.prod_lockdown import (
            clear_bypass,
            clear_hotfix_signal,
            clear_release_signal,
        )

        clear_bypass(session_id)
        clear_release_signal(session_id)
        clear_hotfix_signal(session_id)
        clear_branch_signal(session_id)
        clear_pr_signal(session_id)
    except Exception:
        pass

    # Clear voice output flag for this session
    try:
        from hooks.context.voice_output import clear_voice_enabled

        clear_voice_enabled(session_id)
    except Exception:
        pass

    # Clear controls bypass IF this session is the owner (subagent ends are no-ops)
    try:
        from hooks.context.controls_toggle import clear_controls_disabled

        clear_controls_disabled(session_id)
    except Exception:
        pass

    # --- Broadcast: deregister session + prune dead peers ---
    from hooks.config import BROADCAST_ENABLED

    if BROADCAST_ENABLED:
        try:
            from hooks.context.broadcast import heartbeat_sessions, mark_session_closed

            mark_session_closed(session_id)
            heartbeat_sessions()
        except Exception as e:
            log("broadcast session_end failed", {"error": str(e)})


def on_user_prompt_submit(payload: dict) -> None:
    """Handle UserPromptSubmit event."""
    from hooks.config import SECRETS_MODE

    session_id = payload.get("session_id", "")
    log("User prompt submitted", {"session_id": session_id})

    # --- Secrets scanning ---
    if SECRETS_MODE != "off":
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
    else:
        log("Secrets scanning skipped (mode=off)")

    # --- Brain adapter: counter-gated refresh of brain content ---
    try:
        from hooks.config import BRAIN_ENABLED

        if BRAIN_ENABLED:
            from hooks.context.brain_adapter import maybe_refresh as brain_maybe_refresh

            brain_maybe_refresh(session_id)
    except Exception as e:
        log("brain_adapter refresh failed", {"error": str(e)})

    # --- CI Manifesto: counter-gated re-injection of doctrine ---
    try:
        from hooks.config import CI_MANIFESTO_ENABLED

        if CI_MANIFESTO_ENABLED:
            from hooks.context.ci_manifesto import maybe_refresh as ci_manifesto_refresh

            ci_manifesto_refresh(session_id)
    except Exception as e:
        log("ci_manifesto refresh failed", {"error": str(e)})

    # --- Rules refresh: one-shot re-injection for running sessions ---
    try:
        from hooks.context.rules_refresh import maybe_inject as rules_refresh_inject

        rules_refresh_inject(session_id)
    except Exception as e:
        log("rules_refresh inject failed", {"error": str(e)})

    # --- Amygdala: emergency signal check (every turn, O(1) stat) ---
    try:
        from hooks.config import AMYGDALA_ENABLED

        if AMYGDALA_ENABLED:
            from hooks.context.amygdala_hook import check_amygdala

            check_amygdala(session_id)
    except Exception as e:
        log("amygdala_hook failed", {"error": str(e)})

    # --- Prod lockdown bypass: detect operator override phrase ---
    try:
        from hooks.context.prod_lockdown import contains_bypass_phrase, set_bypass

        prompt = payload.get("prompt", "")
        if prompt and contains_bypass_phrase(prompt):
            set_bypass(session_id)
            log("prod_lockdown: bypass active this turn", {"session_id": session_id})
    except Exception as e:
        log("prod_lockdown bypass detection failed", {"error": str(e)})

    # --- Release-gate / hotfix / branch / PR signals (CI Manifesto §9, §14, §15) ---
    try:
        from hooks.context.branch_guard import set_branch_signal, set_pr_signal
        from hooks.context.ci_manifesto import (
            contains_branch_signal,
            contains_hotfix_signal,
            contains_pr_signal,
            contains_release_signal,
        )
        from hooks.context.prod_lockdown import set_hotfix_signal, set_release_signal

        prompt = payload.get("prompt", "")
        if prompt:
            if session_id not in _KNOWN_SUBAGENT_IDS and contains_release_signal(prompt):
                set_release_signal(session_id)
                log(
                    "ci_manifesto: release-gate signal active this turn",
                    {"session_id": session_id},
                )
            if session_id not in _KNOWN_SUBAGENT_IDS and contains_hotfix_signal(prompt):
                set_hotfix_signal(session_id)
                log(
                    "ci_manifesto: hotfix signal active this session",
                    {"session_id": session_id},
                )
            if contains_branch_signal(prompt):
                set_branch_signal(session_id)
                log(
                    "ci_manifesto: branch-creation signal active this turn",
                    {"session_id": session_id},
                )
            if session_id not in _KNOWN_SUBAGENT_IDS and contains_pr_signal(prompt):
                set_pr_signal(session_id)
                log(
                    "ci_manifesto: PR-creation signal active this turn",
                    {"session_id": session_id},
                )
    except Exception as e:
        log("ci_manifesto signal detection failed", {"error": str(e)})

    # --- Voice output: detect enable/disable voice signals ---
    try:
        from hooks.config import VOICE_ENABLED

        if VOICE_ENABLED:
            from hooks.context.voice_output import (
                clear_voice_enabled,
                contains_disable_signal,
                contains_enable_signal,
                set_voice_enabled,
            )

            prompt = payload.get("prompt", "")
            if prompt:
                if contains_enable_signal(prompt):
                    set_voice_enabled(session_id)
                    log(
                        "voice_output: voice enabled for session",
                        {"session_id": session_id},
                    )
                    from hooks.common import inject_banner

                    inject_banner(
                        "VOICE",
                        "Voice output ENABLED. All responses will be spoken aloud. Say 'disable voice' to stop. This is a system toggle — no action needed from you.",
                    )
                elif contains_disable_signal(prompt):
                    clear_voice_enabled(session_id)
                    log(
                        "voice_output: voice disabled for session",
                        {"session_id": session_id},
                    )
                    from hooks.common import inject_banner

                    inject_banner(
                        "VOICE",
                        "Voice output DISABLED. Responses will no longer be spoken. This is a system toggle — no action needed from you.",
                    )
            # Check quota banner (even if no enable/disable signal this turn)
            from hooks.context.voice_output import check_quota_banner

            quota_msg = check_quota_banner(session_id)
            if quota_msg:
                from hooks.common import inject_banner

                inject_banner("VOICE", quota_msg)
    except Exception as e:
        log("voice_output signal detection failed", {"error": str(e)})

    # --- Controls toggle: detect "disable controls" / "enable controls" ---
    try:
        from hooks.config import CONTROLS_BYPASS_ENABLED

        if CONTROLS_BYPASS_ENABLED:
            from hooks.context.controls_toggle import (
                clear_controls_disabled,
                is_controls_disabled,
                set_controls_disabled,
            )
            from hooks.context.controls_toggle import (
                contains_disable_signal as _ctl_disable,
            )
            from hooks.context.controls_toggle import (
                contains_enable_signal as _ctl_enable,
            )

            prompt = payload.get("prompt", "")
            if prompt:
                from hooks.common import inject_banner

                if _ctl_disable(prompt):
                    set_controls_disabled(session_id)
                    log(
                        "controls_toggle: bypass mode ACTIVE",
                        {"session_id": session_id},
                    )
                    inject_banner(
                        "CONTROLS",
                        "Bypass mode ACTIVE — branch/PR/release-merge/hotfix gates short-circuited "
                        "for this session and all spawned subagents. Direct push to main and "
                        "commit-on-main remain blocked. Say 'enable controls' to restore.",
                    )
                elif _ctl_enable(prompt):
                    clear_controls_disabled(session_id, force=True)
                    log(
                        "controls_toggle: bypass mode CLEARED",
                        {"session_id": session_id},
                    )
                    inject_banner(
                        "CONTROLS",
                        "Bypass mode DISABLED — CI-manifesto signal gates restored. "
                        "Branch/PR/release ops require their normal signals.",
                    )
                elif is_controls_disabled(session_id):
                    inject_banner(
                        "CONTROLS",
                        "Bypass mode is ACTIVE this session. Say 'enable controls' to restore gates.",
                    )
    except Exception as e:
        log("controls_toggle signal detection failed", {"error": str(e)})

    # --- Broadcast: inject pending messages ---
    from hooks.config import BROADCAST_ENABLED

    if BROADCAST_ENABLED:
        try:
            from hooks.context.broadcast import check_and_inject_broadcasts

            check_and_inject_broadcasts(session_id)
        except Exception as e:
            log("broadcast user_prompt failed", {"error": str(e)})


def on_pre_tool_use(payload: dict) -> None:
    """Handle PreToolUse event."""
    from hooks.config import SECRETS_MODE

    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {})

    if SECRETS_MODE == "off":
        log(
            f"Pre tool use: {tool_name} (secrets scanning skipped, mode=off)",
            {"tool": tool_name},
        )
    else:
        from hooks.secrets import redact, scan

        # Bypass mode lifts the secrets-in-files block (operator decides when to allow it).
        # Detection still runs — we log and emit telemetry so the operator can audit.
        try:
            from hooks.context.controls_toggle import is_controls_disabled

            _secrets_bypass = is_controls_disabled(payload.get("session_id", ""))
        except Exception:
            _secrets_bypass = False

        # Log command details for Bash
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            safe_command = redact(command, mode=SECRETS_MODE)[:500]
            log(
                f"Pre tool use: {tool_name}",
                {"tool": tool_name, "command": safe_command},
            )
            hits = scan(command, mode=SECRETS_MODE)
            if hits:
                names = ", ".join(hits)
                # Detect file-write operators: >, >>, tee, dd of=
                # If the command is writing secrets to a file, block.
                import re as _re

                _writes_file = bool(
                    _re.search(r"(?<![<&])>\s*['\"]?[^>&|\s]", command)
                    or _re.search(r">>", command)
                    or _re.search(r"\btee\b", command)
                    or _re.search(r"\bdd\b[^|&;\n]*of=", command)
                )
                otel.emit_event(
                    _OTEL_SECRET_DETECTED,
                    {
                        "session.id": payload.get("session_id", ""),
                        "tool_name": tool_name,
                        "secret_types": names,
                        "action": "block" if _writes_file else "warn",
                        "writes_file": _writes_file,
                    },
                )
                if _writes_file:
                    if _secrets_bypass:
                        from hooks.common import inject_context

                        inject_context(
                            f"NOTE: Secret(s) detected in Bash command writing to a file "
                            f"({names}) — bypass mode ACTIVE, write allowed. "
                            "Operator authorized via 'disable controls'."
                        )
                    else:
                        raise BlockAction(
                            f"BLOCKED: Secret(s) detected in Bash command writing to a file ({names}). "
                            "Secrets must never be committed to code or config files. "
                            "Use environment variables, a vault, or operator-managed secret files. "
                            "To override for this session: say 'disable controls'."
                        )
                # Inline secrets (no file write): scan + log + note, don't block.
                # The operator handles transcript secrecy locally (closed system).
                from hooks.common import inject_context

                inject_context(
                    f"NOTE: Secret(s) detected inline in Bash command ({names}). "
                    "Logged and noted — transcript secrecy is operator-managed. "
                    "Do not echo values back or persist them."
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
                elif _secrets_bypass:
                    otel.emit_event(
                        _OTEL_SECRET_DETECTED,
                        {
                            "session.id": payload.get("session_id", ""),
                            "tool_name": tool_name,
                            "secret_types": names,
                            "action": "warn",
                            "bypass": True,
                        },
                    )
                    from hooks.common import inject_context

                    inject_context(
                        f"NOTE: Secret(s) detected in {tool_name} content ({names}) — "
                        "bypass mode ACTIVE, write allowed. "
                        "Operator authorized via 'disable controls'."
                    )
                else:
                    otel.emit_event(
                        _OTEL_SECRET_DETECTED,
                        {
                            "session.id": payload.get("session_id", ""),
                            "tool_name": tool_name,
                            "secret_types": names,
                            "action": "block",
                        },
                    )
                    raise BlockAction(
                        f"BLOCKED: Secret(s) detected in {tool_name} content ({names}). "
                        "Never write credential values to files. "
                        "Use environment variables instead. "
                        "To override for this session: say 'disable controls'."
                    )
        else:
            log(f"Pre tool use: {tool_name}", {"tool": tool_name})

    # --- Version guard: block version field edits in project manifests ---
    if tool_name in ("Edit", "Write"):
        try:
            from hooks.context.version_guard import check_version_guard

            check_version_guard(payload)
        except BlockAction:
            raise
        except Exception as e:
            log("version_guard check failed", {"error": str(e)})

    # --- Branch guard: block git operations targeting main/master ---
    if tool_name == "Bash":
        try:
            from hooks.context.branch_guard import (
                check_branch_guard,
                check_commit_on_main,
            )

            check_branch_guard(payload)
            check_commit_on_main(payload)
        except BlockAction:
            raise
        except Exception as e:
            log("branch_guard check failed", {"error": str(e)})
            print(
                f"WARNING: branch_guard check failed ({e}) — guard bypassed",
                file=sys.stderr,
                flush=True,
            )

    # --- kubectl mutation guard: HARD FLOOR — block live-system state mutation ---
    if tool_name == "Bash":
        try:
            from hooks.config import KUBECTL_MUTATION_GUARD_ENABLED

            if KUBECTL_MUTATION_GUARD_ENABLED:
                from hooks.context.kubectl_mutation_guard import check_kubectl_mutation

                check_kubectl_mutation(payload)
        except BlockAction:
            raise
        except Exception as e:
            log("kubectl_mutation_guard check failed", {"error": str(e)})
            print(
                f"WARNING: kubectl_mutation_guard check failed ({e}) — guard bypassed",
                file=sys.stderr,
                flush=True,
            )

    # --- Dependency install banner (supply chain defense) ---
    if tool_name == "Bash":
        try:
            from hooks.context.dep_banner import check_dep_install

            check_dep_install(payload)
        except Exception as e:
            log("dep_banner check failed", {"error": str(e)})

    # --- Prod lockdown: block production operations unless bypass active ---
    if tool_name == "Bash":
        try:
            from hooks.context.prod_lockdown import check_prod_lockdown

            check_prod_lockdown(payload)
        except BlockAction:
            raise
        except Exception as e:
            log("prod_lockdown check failed", {"error": str(e)})
            print(
                f"WARNING: prod_lockdown check failed ({e}) — guard bypassed",
                file=sys.stderr,
                flush=True,
            )

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
            otel.emit_event(
                "agentihooks.guardrail.read_deduplicated",
                {
                    "session.id": payload.get("session_id", ""),
                    "file_path": payload.get("tool_input", {}).get("file_path", ""),
                },
            )
            raise
        except Exception as e:
            log("file_read_cache check failed", {"error": str(e)})

    # CLAUDE.md sanity check — block edits that would bloat CLAUDE.md
    if tool_name in ("Write", "Edit"):
        try:
            from hooks.config import CLAUDE_MD_SANITY_CHECK

            if CLAUDE_MD_SANITY_CHECK:
                from hooks.context.claude_md_sanity import check_claude_md_write

                check_claude_md_write(payload)
        except BlockAction:
            otel.emit_event(
                "agentihooks.guardrail.claude_md_blocked",
                {
                    "session.id": payload.get("session_id", ""),
                    "tool_name": tool_name,
                    "file_path": payload.get("tool_input", {}).get("file_path", ""),
                },
            )
            raise
        except Exception as e:
            log("claude_md_sanity check failed", {"error": str(e)})

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
            otel.emit_event(
                "agentihooks.guardrail.retry_blocked",
                {
                    "session.id": payload.get("session_id", ""),
                    "tool_name": payload.get("tool_name", "unknown"),
                },
            )
            _tracer = otel.get_tracer()
            if _tracer:
                with _tracer.start_as_current_span(
                    "agentihooks.guardrail.retry_blocked",
                    attributes={
                        "session.id": payload.get("session_id", ""),
                        "tool_name": payload.get("tool_name", "unknown"),
                    },
                ):
                    pass
            raise
        except Exception as e:
            log("retry_breaker pre-tool failed", {"error": str(e)})

    # --- Combined PreToolUse context injection: broadcast + enforcement ---
    # Both sources must merge into a SINGLE hookSpecificOutput JSON; emitting
    # two separate JSON lines makes Claude Code drop the second.
    from hooks.config import BROADCAST_CRITICAL_ON_PRETOOL, BROADCAST_ENABLED, ENFORCEMENT_INJECTION_ENABLED

    _pretool_blocks: list[str] = []

    if BROADCAST_ENABLED and BROADCAST_CRITICAL_ON_PRETOOL:
        try:
            from hooks.context.broadcast import get_pretool_context

            _broadcast_ctx = get_pretool_context(session_id)
            if _broadcast_ctx:
                _pretool_blocks.append(_broadcast_ctx)
        except Exception as e:
            log("broadcast pretool failed", {"error": str(e)})

    if ENFORCEMENT_INJECTION_ENABLED:
        try:
            from hooks.context.enforcement import get_pretool_enforcements

            _enf_ctx = get_pretool_enforcements(session_id)
            if _enf_ctx:
                _pretool_blocks.append(_enf_ctx)
        except Exception as e:
            log("enforcement pretool failed", {"error": str(e)})

    if _pretool_blocks:
        import json as _json

        print(
            _json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": "\n\n".join(_pretool_blocks),
                    }
                }
            )
        )


def _trace_mark(phase: str, tool_name: str, session_id: str = "") -> None:
    """Emit a 'phase entered' marker to hooks.log for random-stop bisection.

    No-op unless POST_TOOL_TRACE=1. Cheap (single log call). Order in
    hooks.log = order phases ran. The last marker before a stop = the
    handler that swallowed the turn.
    """
    from hooks.config import POST_TOOL_TRACE

    if not POST_TOOL_TRACE:
        return
    log(
        f"post_tool_trace: phase={phase}",
        {"tool": tool_name, "session_id": session_id, "t_ns": time.perf_counter_ns()},
    )


@contextmanager
def _trace_phase(phase: str, tool_name: str, session_id: str = ""):
    """Wrap a sub-handler with entry/exit/duration logs.

    Use only on phases whose body is a single try/except block at the
    function's top indent level (so the `with` adds one level cleanly).
    Active only when POST_TOOL_TRACE=1.
    """
    from hooks.config import POST_TOOL_TRACE

    if not POST_TOOL_TRACE:
        yield
        return

    t0 = time.perf_counter()
    log(f"post_tool_trace: ENTER {phase}", {"tool": tool_name, "session_id": session_id})
    try:
        yield
    except BaseException as e:
        dt_ms = (time.perf_counter() - t0) * 1000
        log(
            f"post_tool_trace: RAISE {phase}",
            {"tool": tool_name, "duration_ms": round(dt_ms, 2), "error": f"{type(e).__name__}: {e}"},
        )
        raise
    else:
        dt_ms = (time.perf_counter() - t0) * 1000
        log(
            f"post_tool_trace: EXIT {phase}",
            {"tool": tool_name, "duration_ms": round(dt_ms, 2)},
        )


def on_post_tool_use(payload: dict) -> None:
    """Handle PostToolUse event."""
    tool_name = payload.get("tool_name", "unknown")
    log(f"Post tool use: {tool_name}", {"tool": tool_name})
    _trace_session_id = payload.get("session_id", "")

    # --- AskUserQuestion answers feed signal detection (CI Manifesto §9, §14, §15) ---
    if tool_name == "AskUserQuestion":
        _trace_mark("ask_user_question_signals", tool_name, _trace_session_id)
        try:
            from hooks.context.branch_guard import set_branch_signal, set_pr_signal
            from hooks.context.ci_manifesto import (
                contains_branch_signal,
                contains_hotfix_signal,
                contains_pr_signal,
                contains_release_signal,
            )
            from hooks.context.prod_lockdown import (
                set_hotfix_signal,
                set_release_signal,
            )

            session_id = payload.get("session_id", "")
            resp = payload.get("tool_response") or payload.get("tool_output") or {}
            texts: list[str] = []
            if isinstance(resp, dict):
                answers = resp.get("answers") or {}
                if isinstance(answers, dict):
                    texts.extend(str(v) for v in answers.values())
                ann = resp.get("annotations") or {}
                if isinstance(ann, dict):
                    for a in ann.values():
                        if isinstance(a, dict):
                            if a.get("notes"):
                                texts.append(str(a["notes"]))
                            if a.get("preview"):
                                texts.append(str(a["preview"]))
            combined = " ".join(texts).lower()
            if combined and session_id:
                if contains_release_signal(combined):
                    set_release_signal(session_id)
                    log(
                        "ci_manifesto: release-gate signal via AskUserQuestion answer",
                        {"session_id": session_id},
                    )
                if contains_hotfix_signal(combined):
                    set_hotfix_signal(session_id)
                    log(
                        "ci_manifesto: hotfix signal via AskUserQuestion answer (session-scoped)",
                        {"session_id": session_id},
                    )
                if contains_branch_signal(combined):
                    set_branch_signal(session_id)
                    log(
                        "ci_manifesto: branch signal via AskUserQuestion answer",
                        {"session_id": session_id},
                    )
                if contains_pr_signal(combined):
                    set_pr_signal(session_id)
                    log(
                        "ci_manifesto: PR signal via AskUserQuestion answer",
                        {"session_id": session_id},
                    )
                try:
                    from hooks.config import CONTROLS_BYPASS_ENABLED

                    if CONTROLS_BYPASS_ENABLED:
                        from hooks.context.controls_toggle import (
                            clear_controls_disabled,
                            set_controls_disabled,
                        )
                        from hooks.context.controls_toggle import (
                            contains_disable_signal as _ctl_disable,
                        )
                        from hooks.context.controls_toggle import (
                            contains_enable_signal as _ctl_enable,
                        )

                        if _ctl_disable(combined):
                            set_controls_disabled(session_id)
                            log(
                                "controls_toggle: bypass via AskUserQuestion",
                                {"session_id": session_id},
                            )
                        elif _ctl_enable(combined):
                            clear_controls_disabled(session_id, force=True)
                            log(
                                "controls_toggle: cleared via AskUserQuestion",
                                {"session_id": session_id},
                            )
                except Exception as e:
                    log("controls_toggle AskUserQuestion failed", {"error": str(e)})
        except Exception as e:
            log(
                "ci_manifesto AskUserQuestion signal detection failed",
                {"error": str(e)},
            )

    # Log transcript entries to hooks.log (for debugging)
    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    if session_id and transcript_path:
        _trace_mark("transcript_logger", tool_name, _trace_session_id)
        from hooks.observability.transcript import log_new_entries

        log_new_entries(session_id, transcript_path)

    # Bash output filtering
    if tool_name == "Bash":
        _trace_mark("bash_output_filter", tool_name, _trace_session_id)
        try:
            from hooks.config import BASH_FILTER_ENABLED

            if BASH_FILTER_ENABLED:
                from hooks.context.bash_output_filter import filter_bash_output

                filtered = filter_bash_output(
                    tool_name,
                    payload.get("tool_input", {}),
                    payload.get("tool_output", ""),
                )
                if filtered is not None:
                    import json as _json

                    otel.emit_event(
                        "agentihooks.context.output_filtered",
                        {
                            "session.id": payload.get("session_id", ""),
                            "tool_name": tool_name,
                        },
                    )
                    # Apply token compression if scope=all
                    try:
                        from hooks.config import CONTEXT_COMPRESSION_SCOPE

                        if CONTEXT_COMPRESSION_SCOPE == "all":
                            from hooks.context.preprocessor import (
                                get_level_from_config,
                                preprocess,
                            )

                            level = get_level_from_config()
                            if level > 0:
                                filtered = preprocess(filtered, level)
                    except Exception:
                        pass
                    print(_json.dumps({"additionalContext": filtered}))
        except Exception as e:
            log("bash_output_filter failed", {"error": str(e)})

    # Mark file as read in cache
    if tool_name == "Read":
        _trace_mark("file_read_cache_mark", tool_name, _trace_session_id)
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
    _trace_mark("tool_memory_record_error", tool_name, _trace_session_id)
    from hooks.tool_memory import record_error

    record_error(payload)

    # Emit OTEL event if error was recorded
    is_error = payload.get("is_error", False)
    exit_code = payload.get("tool_input", {}).get("exitCode")
    if is_error or (exit_code and str(exit_code) != "0"):
        _trace_mark("otel_error_event", tool_name, _trace_session_id)
        otel.emit_event(
            "agentihooks.error.recorded",
            {
                "session.id": payload.get("session_id", ""),
                "tool_name": payload.get("tool_name", "unknown"),
            },
        )

    # Retry circuit breaker — track failures and inject research instructions
    from hooks.config import RETRY_BREAKER_ENABLED

    if RETRY_BREAKER_ENABLED:
        _trace_mark("retry_breaker", tool_name, _trace_session_id)
        try:
            from hooks.context.retry_breaker import on_post_tool_result

            on_post_tool_result(payload)
        except Exception as e:
            log("retry_breaker post-tool failed", {"error": str(e)})

    # Context audit — record tool output size
    _trace_mark("context_audit", tool_name, _trace_session_id)
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
    if tool_name == "Agent":
        _trace_mark("thinking_policy", tool_name, _trace_session_id)
    try:
        from hooks.config import DEFAULT_EFFORT, EFFORT_POLICY_ENABLED

        if EFFORT_POLICY_ENABLED and tool_name == "Agent":
            from hooks.common import inject_context as _inject_effort
            from hooks.context.thinking_policy import check_subagent_effort

            warning = check_subagent_effort(payload.get("tool_input", {}), DEFAULT_EFFORT)
            if warning:
                _inject_effort(warning)
    except Exception as e:
        log("thinking_policy check failed", {"error": str(e)})

    _trace_mark("post_tool_use_done", tool_name, _trace_session_id)


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
    otel.emit_event(
        "agentihooks.session.ended",
        {
            "session.id": session_id,
            "num_turns": str(metrics.get("num_turns", 0)),
            "duration_ms": str(metrics.get("duration_ms", 0)),
        },
    )

    # Brain writer — scan transcript for agent-emitted markers + publish.
    # Forked: transcript scan + per-marker SSH/HTTP publish can take 10s+.
    try:
        from hooks.config import BRAIN_WRITER_ENABLED

        if BRAIN_WRITER_ENABLED and (transcript_path or payload.get("last_assistant_message")):
            from hooks._async import fork_and_call
            from hooks.context.brain_writer_hook import write_markers

            last_msg = payload.get("last_assistant_message", "")
            fork_and_call(
                write_markers,
                session_id,
                transcript_path,
                last_message=last_msg,
                timeout_sec=60,
                task_name="brain_writer",
            )
    except Exception as e:
        log("brain_writer_hook dispatch failed", {"error": str(e)})

    # Emit a trace span for session end (visible in Langfuse)
    tracer = otel.get_tracer()
    if tracer:
        with tracer.start_as_current_span(
            "agentihooks.session.stop",
            attributes={
                "session.id": session_id,
                "num_turns": metrics.get("num_turns", 0),
                "duration_ms": metrics.get("duration_ms", 0),
            },
        ):
            pass

    # Check for errors and notify — forked: full transcript scan + SMTP/HTTP
    # email send can take 5-10s and MUST NOT block session cleanup.
    try:
        from hooks._async import fork_and_call

        fork_and_call(
            notify_on_error,
            transcript_path,
            timeout_sec=30,
            task_name="notify_on_error",
        )
    except Exception as e:
        log("notify_on_error dispatch failed", {"error": str(e)})

    # Clear per-turn signals only; session-scoped signals (PR, release, hotfix)
    # persist until on_session_end
    try:
        from hooks.context.branch_guard import clear_branch_signal
        from hooks.context.prod_lockdown import clear_bypass

        clear_bypass(session_id)
        clear_branch_signal(session_id)
    except Exception as e:
        log("prod_lockdown.clear_bypass failed", {"error": str(e)})

    # Scan transcript for MCP errors missed by PostToolUse
    from hooks.tool_memory import scan_transcript

    scan_transcript(payload)

    # Log transcript entries to hooks.log — forked: Redis GET/SETEX round-trips
    # + full transcript line scan can take 1-2s.
    if session_id and transcript_path:
        try:
            from hooks._async import fork_and_call
            from hooks.observability.transcript import log_new_entries

            fork_and_call(
                log_new_entries,
                session_id,
                transcript_path,
                timeout_sec=15,
                task_name="transcript_log",
            )
        except Exception as e:
            log("log_new_entries dispatch failed", {"error": str(e)})

    # Auto-save session memory — forked: reads full transcript + Redis write
    # (or blocks on Redis connection timeout if unreachable). Can take 3-8s.
    try:
        from hooks.config import MEMORY_AUTO_SAVE

        if MEMORY_AUTO_SAVE and session_id and transcript_path:
            from hooks._async import fork_and_call
            from hooks.memory.auto_save import auto_save_session

            fork_and_call(
                auto_save_session,
                session_id,
                transcript_path,
                timeout_sec=30,
                task_name="auto_save",
            )
    except Exception as e:
        log("Memory auto-save dispatch failed", {"error": str(e)})

    # Context audit — emit report if fill_pct exceeds threshold
    try:
        from hooks.config import CONTEXT_AUDIT_ENABLED, CONTEXT_AUDIT_THRESHOLD_PCT

        if CONTEXT_AUDIT_ENABLED and session_id:
            from hooks.observability.context_audit import (
                format_audit_report,
                get_audit_summary,
            )
            from hooks.observability.token_monitor import get_context_fill_pct

            fill_pct = get_context_fill_pct(payload)
            if fill_pct is not None and fill_pct >= CONTEXT_AUDIT_THRESHOLD_PCT:
                summary = get_audit_summary(session_id)
                if summary:
                    report = format_audit_report(summary, fill_pct)
                    if report:
                        log(
                            "Context audit report",
                            {"fill_pct": fill_pct, "report": report},
                        )
    except Exception as e:
        log("context_audit report failed", {"error": str(e)})

    # Voice output — summarize + speak last assistant message
    try:
        from hooks.config import VOICE_ENABLED

        if VOICE_ENABLED:
            from hooks.context.voice_output import maybe_speak

            maybe_speak(session_id, payload.get("last_assistant_message", ""))
    except Exception as e:
        log("voice_output stop hook failed", {"error": str(e)})


def on_subagent_start(payload: dict) -> None:
    """Handle SubagentStart event — wire the brain through to subagents.

    Mirrors on_session_start brain logic so dispatched subagents get:
      - brain context injected via brain_adapter
      - broadcasts delivered via broadcast.check_and_inject_broadcasts
      - brain.inject + brain.delivery spans visible in ClickHouse/Langfuse

    This is the fix for the "subagents have no brain" gap (BRAIN-MVP item #21).
    """
    agent_id = payload.get("agent_id") or payload.get("session_id", "")
    agent_type = payload.get("agent_type", "unknown")
    _KNOWN_SUBAGENT_IDS.add(agent_id)
    log(
        "Subagent started",
        {
            "agent_id": agent_id,
            "agent_type": agent_type,
            "otel_endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", _UNSET),
            "otel_protocol": os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", _UNSET),
            "otel_hooks_enabled": os.environ.get("OTEL_HOOKS_ENABLED", _UNSET),
        },
    )

    # CI Manifesto — inject doctrine into subagents too
    try:
        from hooks.config import CI_MANIFESTO_ENABLED

        if CI_MANIFESTO_ENABLED:
            from hooks.context.ci_manifesto import (
                inject_on_session_start as inject_ci_manifesto,
            )

            inject_ci_manifesto()
    except Exception as e:
        log("ci_manifesto subagent_start failed", {"error": str(e)})

    # Brain adapter — publish brain content to the shared broadcast channel
    try:
        from hooks.config import BRAIN_ENABLED

        if BRAIN_ENABLED:
            from hooks.context.brain_adapter import inject_on_session_start

            inject_on_session_start()
    except Exception as e:
        log("brain_adapter subagent_start failed", {"error": str(e)})

    # Broadcast — register subagent session and deliver pending broadcasts
    try:
        from hooks.config import BROADCAST_ENABLED

        if BROADCAST_ENABLED and agent_id:
            from hooks.context.broadcast import (
                check_and_inject_broadcasts,
                register_session,
            )

            register_session(
                agent_id,
                pid=os.getppid(),
                cwd=payload.get("cwd", ""),
                model=payload.get("model", ""),
            )
            check_and_inject_broadcasts(agent_id)
    except Exception as e:
        log("broadcast subagent_start failed", {"error": str(e)})


def on_subagent_stop(payload: dict) -> None:
    """Handle SubagentStop event — capture brain markers from subagent output."""
    agent_id = payload.get("agent_id") or payload.get("session_id", "")
    transcript_path = payload.get("agent_transcript_path") or payload.get("transcript_path", "")
    last_msg = payload.get("last_assistant_message", "")
    log("Subagent stopped", {"agent_id": agent_id})

    # Brain writer — forked (same as on_stop).
    try:
        from hooks.config import BRAIN_WRITER_ENABLED

        if BRAIN_WRITER_ENABLED and (transcript_path or last_msg):
            from hooks._async import fork_and_call
            from hooks.context.brain_writer_hook import write_markers

            fork_and_call(
                write_markers,
                agent_id,
                transcript_path,
                last_message=last_msg,
                timeout_sec=60,
                task_name="brain_writer_subagent",
            )
    except Exception as e:
        log("brain_writer subagent_stop dispatch failed", {"error": str(e)})

    # Clear any signals set during the subagent's lifetime
    try:
        from hooks.context.branch_guard import clear_branch_signal, clear_pr_signal
        from hooks.context.prod_lockdown import clear_bypass, clear_release_signal

        clear_bypass(agent_id)
        clear_release_signal(agent_id)
        clear_branch_signal(agent_id)
        clear_pr_signal(agent_id)
    except Exception:
        pass

    # Log transcript entries to hooks.log — forked (Redis GET/SETEX).
    if agent_id and transcript_path:
        try:
            from hooks._async import fork_and_call
            from hooks.observability.transcript import log_new_entries

            fork_and_call(
                log_new_entries,
                agent_id,
                transcript_path,
                timeout_sec=15,
                task_name="transcript_log_subagent",
            )
        except Exception as e:
            log("log_new_entries subagent dispatch failed", {"error": str(e)})


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

# Track subagent session IDs to prevent signal self-arming (audit finding 6.2)
_KNOWN_SUBAGENT_IDS: set[str] = set()

EVENT_HANDLERS = {
    "SessionStart": on_session_start,
    "SessionEnd": on_session_end,
    "UserPromptSubmit": on_user_prompt_submit,
    "PreToolUse": on_pre_tool_use,
    "PostToolUse": on_post_tool_use,
    "Stop": on_stop,
    "SubagentStart": on_subagent_start,
    "SubagentStop": on_subagent_stop,
    "Notification": on_notification,
    "PreCompact": on_pre_compact,
    "PermissionRequest": on_permission_request,
}


def main() -> None:
    """Main entry point - routes events to handlers.

    Uses ``os._exit`` instead of ``sys.exit`` to bypass Python's shutdown
    sequence. This is critical: non-daemon OTEL exporter / BatchProcessor
    threads would otherwise block process exit for 10-20s while they drain
    retries against an unreachable collector. The hook's output is already
    flushed by the handlers (stderr for banners, log() files), so skipping
    atexit handlers is safe.
    """
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
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(2)  # blocks the action — skip Python shutdown (OTEL threads)
    except json.JSONDecodeError:
        log("Failed to parse JSON payload")
    except KeyboardInterrupt:
        # Operator pressed Ctrl+C mid-hook (most common during SessionEnd
        # while imports are still resolving). Exit silently — no traceback.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)  # standard SIGINT exit code
    except Exception as e:
        log(f"Hook manager error: {str(e)}")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
