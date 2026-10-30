#!/usr/bin/env python3
"""Hook Manager - Centralized entry point for all Claude Code hooks.

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
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hooks.classifier.child import skip_classifier_child
from hooks.observability import otel

sys.path.insert(0, str(Path(__file__).parent.parent))

from hooks.common import log
from hooks.context import tool_end
from hooks.prompt_budget import PromptBudget


class BlockAction(Exception):
    """Raised by a hook handler to block the current Claude Code action (exit 2)."""


_OTEL_SECRET_DETECTED = "agentihooks.guardrail.secret_detected"
_UNSET = "<unset>"
_QUESTION_CHECK_FAILED = "The question tool is off: the operator presence check failed. Keep working without it."


def log_claude_max_output_tokens():
    """Inject the output token limit into Claude's context window.

    This makes Claude aware of response size constraints so it can proactively
    keep responses under the limit to avoid 'exceeded output token maximum' errors.
    """
    try:
        max_tokens = os.getenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS")

        if max_tokens:
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
    from datetime import datetime

    metrics = {
        "num_turns": 0,
        "duration_ms": None,
        "last_response": None,
    }

    try:
        path = Path(transcript_path)
        if not path.exists():
            return metrics

        from hooks.memory.transcript_reader import detect_transcript_format

        if detect_transcript_format(path) in ("codex", "copilot"):
            from hooks.memory.transcript_reader import iter_transcript_records

            user_turns = 0
            last_text = None
            for rec in iter_transcript_records(path):
                kind = rec.get("kind")
                if kind == "user_text":
                    user_turns += 1
                elif kind in ("assistant_text", "turn_complete") and rec.get("text"):
                    last_text = rec["text"]
            metrics["num_turns"] = user_turns
            metrics["last_response"] = last_text
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

                    timestamp_str = entry.get("timestamp")
                    if timestamp_str:
                        try:
                            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                            if first_timestamp is None:
                                first_timestamp = ts
                            last_timestamp = ts
                        except (ValueError, TypeError):
                            pass

                    if entry_type == "user":
                        user_count += 1

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

        if first_timestamp and last_timestamp:
            duration = last_timestamp - first_timestamp
            metrics["duration_ms"] = int(duration.total_seconds() * 1000)

    except Exception:  # NOSONAR
        pass

    return metrics


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

        content = path.read_text(encoding="utf-8")
        content_lower = content.lower()

        detected_patterns = [p for p in ERROR_PATTERNS if p in content_lower]
        if not detected_patterns:
            return

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

        error_context = "\n\n---\n\n".join(error_lines[:5])
        patterns_found = ", ".join(detected_patterns)

        from hooks.integrations.mailer import (
            scan_for_config_files,
            send_error_notification,
        )

        email_config, template = scan_for_config_files()

        if not email_config:
            log("Email notification skipped: email.json not found in working directory")
            return

        agent_name = os.getenv("AGENT_NAME", "Agent")

        notification_content = f"""# Error Detected in Agent Session

**Agent:** {agent_name}
**Transcript:** `{transcript_path}`
**Patterns Detected:** {patterns_found}

## Error Details

{error_context if error_context else "Error detected in transcript (see logs for full details)"}

---
*Automated notification from {agent_name} hook system.*
"""

        result = send_error_notification(
            config=email_config,
            template=template,
            content=notification_content,
            subject=f"{agent_name} - Error Detected",
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
        log("notify_on_error failed", {"error": str(e)})


def _close_master_coding(session_id: str, source: str | None) -> None:
    if source == "compact":
        return
    try:
        from hooks.context import master_coding

        master_coding.close(session_id)
    except Exception as e:
        log("master coding window close failed", {"error": str(e)})


def _run_start_conditions(payload: dict) -> None:
    try:
        from hooks.context import conditions
        from hooks.targets.capabilities import can_inject_context

        if not can_inject_context("SessionStart"):
            return
        effect = conditions.start_effect(payload)
        if not effect:
            return
        from hooks.common import inject_context

        for ctx in effect.contexts:
            inject_context(ctx)
    except Exception as e:
        log("conditions session_start failed", {"error": str(e)})


def on_session_start(payload: dict) -> None:
    """Handle SessionStart event."""
    session_id = payload.get("session_id", "")
    log("Session started", {"session_id": session_id})
    if payload.get("source") == "compact":
        _inject_refocus(session_id, "compact")
    _close_master_coding(session_id, payload.get("source"))
    from hooks.lifecycle.guard import session_event

    session_event()
    from hooks.context import operator_mode

    operator_mode.reset(payload)
    _run_start_conditions(payload)
    try:
        from hooks.lifecycle.deps_kick import kick as deps_kick

        deps_kick()
    except Exception as e:
        log("deps kick failed", {"error": str(e)})
    from hooks.lifecycle.codex_refresh_kick import kick as codex_refresh_kick

    codex_refresh_kick()
    _swarm_ledger(payload)
    _hive_join(payload, "start")
    otel.emit_event(
        "agentihooks.session.started",
        {
            "session.id": session_id,
            "agent.name": os.environ.get("AGENT_NAME", "unknown"),
        },
    )

    _request_trace_flush(payload, "start")
    from hooks.context import documentation_session

    documentation_session.start(payload)

    tracer = otel.get_tracer()
    if tracer:
        with tracer.start_as_current_span(
            "agentihooks.session.start",
            attributes={
                "session.id": session_id,
                "agent.name": os.environ.get("AGENT_NAME", "unknown"),
            },
        ):
            pass

    log_claude_max_output_tokens()

    from hooks.config import MCP_SESSION_ID_BANNER_ENABLED

    if session_id and MCP_SESSION_ID_BANNER_ENABLED:
        from hooks.common import inject_context as _inject_sid
        from hooks.targets import current_target as _sid_target

        _host = {"codex": "Codex", "copilot": "Copilot CLI"}.get(_sid_target(), "Claude Code")
        _inject_sid(
            f"Your {_host} session_id is `{session_id}`. Pass it as the "
            "`session_id` argument to the agentihooks MCP tools that need caller "
            "identity — channel_acknowledge."
        )

    from hooks.targets import is_codex as _is_codex

    if _is_codex():
        try:
            import json as _sjson

            from hooks.config import AGENTIHOOKS_HOME as _ah_home
            from hooks.config import BASE_CHANNELS as _channels
            from hooks.targets import global_record as _global_record

            _state = _sjson.loads((_ah_home / "state.json").read_text())
            _profile = _global_record(_state).get("profile", "?")
            from hooks.common import inject_context as _inject_ah

            _inject_ah(
                f"agentihooks active on codex — profile: {_profile} | "
                f"channels: {','.join(_channels) if _channels else 'none'} | "
                "guards: secrets, branch, prod-lockdown, kubectl-mutation, retry-breaker"
            )
        except Exception:
            pass

    try:
        from hooks.config import PROJECT_BRIDGE_ENABLED

        if PROJECT_BRIDGE_ENABLED:
            from hooks.context.project_bridge import inject_project_context

            inject_project_context(payload.get("cwd", ""))
    except Exception as e:
        log("project_bridge failed", {"error": str(e)})

    try:
        from hooks.common import inject_context as _inject_enforcements
        from hooks.context.enforcement import get_session_start_enforcements

        enforcement_context = get_session_start_enforcements(session_id, payload.get("cwd", ""))
        if enforcement_context:
            _inject_enforcements(enforcement_context, also_log=False, skip_compression=True, kind="enforcement")
    except Exception as e:
        log("enforcement session start failed", {"error": str(e)})

    try:
        from hooks.context.injection_trace import record_session_start
        from hooks.targets import current_target as _trace_target

        record_session_start(session_id, os.environ, _trace_target())
    except Exception as e:
        log("injection trace session start failed", {"error": str(e)})

    from hooks.config import MCP_HYGIENE_ENABLED

    if MCP_HYGIENE_ENABLED:
        from hooks.common import inject_context
        from hooks.targets import current_target as _hygiene_target

        _hygiene = (
            "TOKEN CONTROL ACTIVE: Multiple MCP servers loaded. "
            "Disable unused servers via /mcp to reduce per-turn token overhead."
        )
        if _hygiene_target() == "claude":
            _hygiene += (
                " Model guidance: use Sonnet 4.6 for implementation; reserve Opus 4.6 for plan mode (Shift+Tab)."
            )
        inject_context(_hygiene)

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

    try:
        from hooks.config import AGENTIHOOKS_FORCE_DEV_BRANCH

        if AGENTIHOOKS_FORCE_DEV_BRANCH:
            from hooks.context.auto_dev_switch import (
                inject_on_session_start as _auto_dev,
            )

            _auto_dev(payload.get("cwd", ""))
    except Exception as e:
        log("auto_dev_switch session_start failed", {"error": str(e)})

    try:
        from hooks.config import CI_MANIFESTO_ENABLED

        if CI_MANIFESTO_ENABLED:
            from hooks.context.ci_manifesto import (
                inject_on_session_start as inject_ci_manifesto,
            )

            inject_ci_manifesto()
    except Exception as e:
        log("ci_manifesto session_start failed", {"error": str(e)})

    try:
        from hooks.context.brain_adapter import inject_on_session_start

        inject_on_session_start(session_id, payload.get("cwd", ""))
    except Exception as e:
        log("brain_adapter session_start failed", {"error": str(e)})

    from hooks.config import BROADCAST_ENABLED

    if BROADCAST_ENABLED:
        try:
            from hooks.context.account_sessions import agent_pid, session_account
            from hooks.context.broadcast import (
                check_and_inject_broadcasts,
                register_session,
            )

            _agent_pid = agent_pid()
            register_session(
                session_id,
                pid=_agent_pid,
                cwd=payload.get("cwd", ""),
                model=payload.get("model", ""),
                account=session_account(_agent_pid),
                name=os.environ.get("AGENTIHOOKS_AGENT_NAME", ""),
            )
            check_and_inject_broadcasts(session_id)
        except Exception as e:
            log("broadcast session_start failed", {"error": str(e)})

    try:
        from hooks.context.voice_output import cleanup_stale_flags

        cleaned = cleanup_stale_flags()
        if cleaned:
            log("voice_output: cleaned stale flags", {"count": cleaned})
    except Exception:
        pass

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
    _hive_join(payload, "end")
    from hooks.lifecycle.guard import session_event

    session_event()

    metrics = parse_transcript_metrics(transcript_path) if transcript_path else {}

    log(
        "Session ended",
        {
            "session_id": session_id,
            "num_turns": metrics.get("num_turns"),
            "duration_ms": metrics.get("duration_ms"),
        },
    )
    _request_trace_flush(payload, "end")
    from hooks.context import documentation_session

    documentation_session.end(payload)

    if session_id and transcript_path:
        from hooks.observability.transcript import log_new_entries

        log_new_entries(session_id, transcript_path)

    from hooks.config import FILE_READ_CACHE_ENABLED

    if FILE_READ_CACHE_ENABLED:
        try:
            from hooks.context.file_read_cache import clear_session_cache

            clear_session_cache(session_id)
        except Exception:
            pass

    from hooks.config import RETRY_BREAKER_ENABLED

    if RETRY_BREAKER_ENABLED:
        try:
            from hooks.context.retry_breaker import clear_session_state

            clear_session_state(session_id)
        except Exception:
            pass

    try:
        from hooks.context.brain_adapter import clear_session_state as _clear_brain

        _clear_brain(session_id)
    except Exception:
        pass

    try:
        from hooks.context.enforcement import reset_session_counter, reset_session_delivery

        reset_session_delivery(session_id)
        reset_session_counter(session_id)
    except Exception:
        pass

    try:
        from hooks.context.quota_usage import clear_session_state as _clear_quota_usage

        _clear_quota_usage(session_id)
    except Exception:
        pass

    try:
        from hooks.context.quota_policy import clear_session_state as _clear_quota_policy

        _clear_quota_policy(session_id)
    except Exception:
        pass

    from hooks.config import CONTEXT_AUDIT_ENABLED

    if CONTEXT_AUDIT_ENABLED:
        try:
            from hooks.observability.context_audit import clear_session_audit

            clear_session_audit(session_id)
        except Exception:
            pass

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
    try:
        from hooks.context.conditions import disarm_gate

        disarm_gate(session_id)
    except Exception:
        pass

    try:
        from hooks.context.voice_output import clear_voice_enabled

        clear_voice_enabled(session_id)
    except Exception:
        pass

    try:
        from hooks.context.controls_toggle import clear_controls_disabled

        clear_controls_disabled(session_id)
    except Exception:
        pass

    try:
        from hooks.context.claude_md_sanity import clear_session_max_lines

        clear_session_max_lines(session_id)
    except Exception:
        pass

    from hooks.config import BROADCAST_ENABLED

    if BROADCAST_ENABLED:
        try:
            from hooks.context.account_sessions import agent_pid
            from hooks.context.broadcast import heartbeat_sessions, mark_session_closed

            mark_session_closed(session_id, agent_pid())
            heartbeat_sessions()
        except Exception as e:
            log("broadcast session_end failed", {"error": str(e)})


def _prompt_broadcasts(session_id: str) -> None:
    try:
        from hooks.context.broadcast import _get_session_channels
        from scripts.swarm_v2 import broadcast_bridge

        broadcast_bridge.claim(session_id, _get_session_channels(session_id), os.environ)
    except Exception as e:
        log("fleet broadcast claim failed", {"error": str(e)})
    try:
        from hooks.context.broadcast import check_and_inject_broadcasts

        check_and_inject_broadcasts(session_id)
    except Exception as e:
        log("broadcast user_prompt failed", {"error": str(e)})


def on_user_prompt_submit(payload: dict) -> None:
    """Handle UserPromptSubmit event."""
    session_id = payload.get("session_id", "")
    log("User prompt submitted", {"session_id": session_id})
    budget = PromptBudget(start=_EVENT_STARTED, keep=(BlockAction,))
    with budget.guarded(_prompt_overrun if _EVENT_STARTED is not None else None):
        _prompt_steps(budget, payload, session_id)


def _prompt_overrun(budget: PromptBudget) -> None:
    from hooks.targets import emitter

    try:
        budget.cut = budget.running
        budget.report()
        emitter.flush("UserPromptSubmit")
        sys.stdout.flush()
        otel.flush()
    finally:
        os._exit(0)


def _prompt_steps(budget: PromptBudget, payload: dict, session_id: str) -> None:
    from hooks.config import BROADCAST_ENABLED

    run = budget.run
    run("secrets", _prompt_secrets, payload)
    run("amygdala", _prompt_amygdala, session_id)
    run("confirm_inbox", _confirm_inbox, session_id)
    run("swarm_heartbeat", _swarm_heartbeat, "working", payload.get("prompt", ""))
    run("swarm_ledger", _swarm_ledger, payload)
    typed = bool(run("operator_words", _operator_words, payload))
    run("operator_mode", _operator_mode, payload, typed)
    run("master_coding", _master_coding, payload, typed)
    run("trace_flush", _request_trace_flush, payload, "prompt")
    run("quota_usage", _prompt_quota_usage, session_id)
    run("quota_policy", _prompt_quota_policy, payload, session_id)
    run("claude_md_cap", _prompt_claude_md_cap, payload, session_id)
    run("enforcements", _prompt_enforcements, payload, session_id)
    run("ci_manifesto", _prompt_ci_manifesto, session_id)
    run("rules_refresh", _prompt_rules_refresh, session_id)
    run("prod_bypass", _prompt_prod_bypass, payload, session_id)
    run("signals", _prompt_signals, payload, session_id, typed)
    if typed:
        run("gate_lifts", _arm_gate_lifts, payload)
    run("voice", _prompt_voice, payload, session_id, typed)
    run("controls", _prompt_controls, payload, session_id, typed)
    if BROADCAST_ENABLED:
        run("broadcasts", _prompt_broadcasts, session_id)
    run("refocus", _inject_refocus, session_id, "prompt")
    run("ledger_decision", _inject_ledger_decision, payload)


def _prompt_quota_usage(session_id: str) -> None:
    try:
        from hooks.config import QUOTA_USAGE_INJECTION_ENABLED

        if QUOTA_USAGE_INJECTION_ENABLED:
            from hooks.common import inject_context as _inject_quota_usage
            from hooks.context.quota_usage import quota_banner

            _quota_banner = quota_banner(session_id)
            if _quota_banner:
                _inject_quota_usage(_quota_banner, also_log=False, skip_compression=True)
    except Exception as e:
        log("quota usage user prompt failed", {"error": str(e)})


def _prompt_quota_policy(payload: dict, session_id: str) -> None:
    try:
        from hooks.config import QUOTA_POLICY_ENABLED

        if QUOTA_POLICY_ENABLED:
            from hooks.common import inject_context as _inject_quota_policy
            from hooks.context.quota_policy import prompt_context, record_push_signal

            if session_id not in _KNOWN_SUBAGENT_IDS and record_push_signal(session_id, payload.get("prompt", "")):
                log("quota policy: operator push-to-100 signal", {"session_id": session_id})
            _policy_text = prompt_context(session_id, payload.get("cwd", ""))
            if _policy_text:
                _inject_quota_policy(_policy_text, also_log=False, skip_compression=True, kind="alert")
    except Exception as e:
        log("quota policy user prompt failed", {"error": str(e)})


def _prompt_secrets(payload: dict) -> None:
    from hooks.config import SECRETS_MODE

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
                    "Ask the user to use environment variables instead.",
                    kind="alert",
                )
    else:
        log("Secrets scanning skipped (mode=off)")


def _prompt_claude_md_cap(payload: dict, session_id: str) -> None:
    try:
        from hooks.context.claude_md_sanity import parse_max_lines_signal, set_session_max_lines

        requested_max_lines = parse_max_lines_signal(payload.get("prompt", ""))
        if requested_max_lines is not None:
            active_max_lines = set_session_max_lines(session_id, requested_max_lines)
            if active_max_lines is not None:
                from hooks.common import inject_banner

                inject_banner(
                    "CLAUDE.MD CAP",
                    f"CLAUDE.md line cap raised to {active_max_lines} for this session.",
                )
    except Exception as e:
        log("claude_md_sanity signal detection failed", {"error": str(e)})


def _prompt_enforcements(payload: dict, session_id: str) -> None:
    try:
        from hooks.common import inject_context as _inject_enforcements
        from hooks.context.enforcement import get_user_prompt_enforcements

        enforcement_context = get_user_prompt_enforcements(session_id, payload.get("cwd", ""))
        if enforcement_context:
            _inject_enforcements(enforcement_context, also_log=False, skip_compression=True, kind="enforcement")
    except Exception as e:
        log("enforcement user prompt failed", {"error": str(e)})


def _prompt_ci_manifesto(session_id: str) -> None:
    try:
        from hooks.config import CI_MANIFESTO_ENABLED

        if CI_MANIFESTO_ENABLED:
            from hooks.context.ci_manifesto import maybe_refresh as ci_manifesto_refresh

            ci_manifesto_refresh(session_id)
    except Exception as e:
        log("ci_manifesto refresh failed", {"error": str(e)})


def _prompt_rules_refresh(session_id: str) -> None:
    try:
        from hooks.context.rules_refresh import maybe_inject as rules_refresh_inject

        rules_refresh_inject(session_id)
    except Exception as e:
        log("rules_refresh inject failed", {"error": str(e)})


def _prompt_amygdala(session_id: str) -> None:
    try:
        from hooks.config import AMYGDALA_ENABLED

        if AMYGDALA_ENABLED:
            from hooks.context.amygdala_hook import check_amygdala

            check_amygdala(session_id)
    except Exception as e:
        log("amygdala_hook failed", {"error": str(e)})


def _prompt_prod_bypass(payload: dict, session_id: str) -> None:
    try:
        from hooks.context.prod_lockdown import contains_bypass_phrase, set_bypass

        prompt = payload.get("prompt", "")
        if prompt and contains_bypass_phrase(prompt):
            set_bypass(session_id)
            log("prod_lockdown: bypass active this turn", {"session_id": session_id})
    except Exception as e:
        log("prod_lockdown bypass detection failed", {"error": str(e)})


def _prompt_signals(payload: dict, session_id: str, typed: bool) -> None:
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
        if prompt and typed:
            from hooks.context.conditions import arm_gate, contains_condition_signal

            if typed and contains_condition_signal(prompt):
                arm_gate(session_id)
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


def _prompt_voice(payload: dict, session_id: str, typed: bool) -> None:
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
            if prompt and typed:
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
            from hooks.context.voice_output import check_quota_banner

            quota_msg = check_quota_banner(session_id)
            if quota_msg:
                from hooks.common import inject_banner

                inject_banner("VOICE", quota_msg)
    except Exception as e:
        log("voice_output signal detection failed", {"error": str(e)})


def _prompt_controls(payload: dict, session_id: str, typed: bool) -> None:
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

                if typed and _ctl_disable(prompt):
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
                elif typed and _ctl_enable(prompt):
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


def _inject_ledger_decision(payload: dict) -> None:
    try:
        from hooks.common import inject_context
        from hooks.context.ledger_decision import directive

        context = directive(payload)
        if context:
            inject_context(context, also_log=False, skip_compression=True, kind="alert")
    except Exception as e:
        log("ledger decision failed", {"error": str(e)})


def _inbox_blocks(session_id: str, cwd: str = "") -> list[str]:
    try:
        from hooks.context.inbox_delivery import pending_context

        context = pending_context(session_id, cwd=cwd)
    except Exception as e:
        log("inbox delivery failed", {"error": str(e)})
        return []
    return [context] if context else []


def _inject_inbox(session_id: str, cwd: str = "") -> None:
    from hooks.common import inject_context

    for context in _inbox_blocks(session_id, cwd):
        inject_context(context, also_log=False, skip_compression=True, kind="inbox")


def _confirm_inbox(session_id: str) -> None:
    try:
        from hooks.context.inbox_delivery import confirm_shown

        confirm_shown(session_id)
    except Exception as e:
        log("inbox confirmation failed", {"error": str(e)})


def _inject_after_tool(payload: dict) -> None:
    from hooks.common import inject_context
    from hooks.context.quota_policy import posttool_context

    session_id, cwd = payload.get("session_id", ""), payload.get("cwd", "")
    _inject_inbox(session_id, cwd)
    _inject_refocus(session_id, "tool")
    for context in _wait_nudge_blocks(payload):
        inject_context(context, also_log=False, skip_compression=True, kind="refocus")
    quota = posttool_context(session_id, payload.get("tool_name"), cwd)
    if quota:
        inject_context(quota, also_log=False, skip_compression=True, kind="alert")


def _swarm_heartbeat(state: str, prompt: str | None = None, payload: dict | None = None) -> None:
    try:
        from hooks.context.swarm_heartbeat import beat, heard, report

        beat(state)
        if prompt is not None:
            heard(prompt)
        if payload is not None:
            report(payload.get("model", ""))
    except Exception as e:
        log("swarm heartbeat failed", {"error": str(e)})


def _swarm_outcome(payload: dict) -> None:
    try:
        from hooks.context.swarm_heartbeat import outcome
        from hooks.observability.consultation import search

        search(payload)
        outcome(payload)
    except Exception as e:
        log("swarm outcome record failed", {"error": str(e)})


def _master_login_checkpoint() -> None:
    try:
        from scripts import master_claude_login

        master_claude_login.checkpoint(os.environ)
    except Exception as e:
        log("master login checkpoint failed", {"error": str(e)})


def _hive_join(payload: dict, event: str) -> None:
    try:
        from hooks.context.hive_join import run

        run(payload, event)
    except Exception as exc:
        log("hive join report failed", {"error": type(exc).__name__})


def _swarm_ledger(payload: dict) -> None:
    try:
        from hooks.context.swarm_ledger import run

        contexts, block = run(payload)
    except Exception as e:
        log("swarm ledger hook failed", {"error": str(e)})
        return
    from hooks.common import inject_context

    for context in contexts:
        inject_context(context, also_log=False, skip_compression=True, kind="refocus")
    if block:
        raise BlockAction(block)


def _swarm_after_tool(payload: dict) -> None:
    _swarm_outcome(payload)
    _swarm_tool_end(payload)
    _hive_join(payload, "tool")
    _swarm_ledger(payload)


def _swarm_tool_end(payload: dict) -> None:
    """Handle PostToolUseFailure too: a failed call still ends, so the stall clock sees it closed."""
    from scripts.swarm.health.activity import finish

    finish(call_id=payload.get("tool_use_id") or "")


def _operator_words(payload: dict) -> bool:
    try:
        from hooks.context.operator_words import typed

        return typed(payload)
    except Exception as e:
        log("operator words record failed", {"error": str(e)})
        return False


def _operator_mode(payload: dict, typed: bool) -> None:
    try:
        from hooks.common import inject_context
        from hooks.context.operator_mode import notice

        text = notice(payload, typed)
        if text:
            inject_context(text, also_log=False, skip_compression=True, kind="alert")
    except Exception as e:
        log("operator mode failed", {"error": str(e)})


def _master_coding(payload: dict, typed: bool) -> None:
    try:
        from hooks.common import inject_context
        from hooks.context import master_coding

        state = master_coding.observe(payload.get("session_id"), payload.get("prompt"), typed)
        if state:
            inject_context(master_coding.NOTICES[state], also_log=False, skip_compression=True, kind="alert")
    except Exception as e:
        log("master coding window failed", {"error": str(e)})


def _operator_question(payload: dict) -> str:
    try:
        from hooks.context import master_question, operator_mode

        return (
            master_question.move(payload)
            or master_question.refusal(payload)
            or operator_mode.question_block(payload.get("tool_name"), payload.get("session_id"))
        )
    except Exception as e:
        log("operator question check failed", {"error": str(e)})
        return _QUESTION_CHECK_FAILED if payload.get("tool_name") == "AskUserQuestion" else ""


def _operator_reminder(payload: dict) -> None:
    try:
        from hooks.common import inject_context
        from hooks.context import operator_mode

        text = operator_mode.reminder(payload.get("session_id"))
        if text:
            inject_context(text, also_log=False, skip_compression=True, kind="alert")
    except Exception as e:
        log("operator reminder failed", {"error": str(e)})


def _arm_gate_lifts(payload: dict) -> None:
    try:
        from scripts.gates.base import Who
        from scripts.gates.entry import GATES
        from scripts.gates.lift import arm_from_prompt

        armed = arm_from_prompt(payload["prompt"], payload.get("session_id"), Who.from_env(), GATES)
    except Exception as e:
        log("gate lift: arming or its ledger record failed", {"error": str(e)})
        return
    if armed:
        log("gates: operator lift armed", {"session_id": payload.get("session_id"), "gates": armed})


def _refocus_blocks(session_id: str, event: str) -> list[str]:
    try:
        from hooks.context.swarm_refocus import refocus_context

        context = refocus_context(session_id, event)
    except Exception as e:
        log("swarm refocus failed", {"error": str(e)})
        return []
    return [context] if context else []


def _inject_refocus(session_id: str, event: str) -> None:
    from hooks.common import inject_context

    for context in _refocus_blocks(session_id, event):
        inject_context(context, also_log=False, skip_compression=True, kind="refocus")


def _wait_nudge_blocks(payload: dict) -> list[str]:
    try:
        from hooks.context.swarm_wait_nudge import nudge

        context = nudge(payload)
    except Exception as e:
        log("swarm wait nudge failed", {"error": str(e)})
        return []
    return [context] if context else []


def _credential_guard(payload: dict, tool_name: str) -> tuple[dict, str] | None:
    """Run the credential read guard on a tool input; a crash in the guard fails closed only when the input
    names a credential path.
    """
    _credential_rewrite: tuple[dict, str] | None = None
    try:
        from hooks.config import CREDENTIAL_GUARD_ENABLED

        if CREDENTIAL_GUARD_ENABLED:
            from hooks.context.credential_guard import evaluate as _credential_evaluate
            from hooks.targets import current_target as _current_target

            _verdict = _credential_evaluate(
                payload,
                allow_rewrite=_current_target() == "claude" and payload.get("permission_mode") == "bypassPermissions",
            )
            if _verdict.block:
                otel.emit_event(
                    "agentihooks.guardrail.credential_read_blocked",
                    {"session.id": payload.get("session_id", ""), "tool_name": tool_name},
                )
                raise BlockAction(_verdict.block)
            if _verdict.rewrite:
                _credential_rewrite = (_verdict.rewrite, _verdict.note or "")
    except BlockAction:
        raise
    except Exception as e:  # NOSONAR
        if _near_credential(payload.get("tool_input") or {}):
            raise BlockAction(
                f"BLOCKED: credential guard internal error near a credential path — refusing ({e})."
            ) from e
        log("credential_guard failed", {"error": str(e)})
        print(f"WARNING: credential_guard check failed ({e}) — guard bypassed", file=sys.stderr)
    return _credential_rewrite


def _plan_read_guard(payload: dict, tool_name: str) -> None:
    try:
        from hooks.context.plan_read_guard import check as _plan_read_check

        _plan_refusal = _plan_read_check(payload)
        if _plan_refusal:
            otel.emit_event(
                "agentihooks.guardrail.plan_read_blocked",
                {"session.id": payload.get("session_id", ""), "tool_name": tool_name},
            )
            raise BlockAction(_plan_refusal)
    except BlockAction:
        raise
    except Exception as e:
        log("plan_read_guard failed", {"error": str(e)})


def _check_shell_guards(payload: dict) -> None:
    from hooks.context.local_test_guard import check_local_tests
    from hooks.context.swarm_wait_nudge import check_github_watch

    check_local_tests(payload)
    check_github_watch(payload)
    if payload.get("tool_name") != "Bash":
        return
    try:
        from hooks.context.branch_guard import check_branch_guard, check_commit_on_main
        from hooks.context.master_coding import protection_refusal

        check_branch_guard(payload)
        check_commit_on_main(payload)
        if refusal := protection_refusal(payload):
            raise BlockAction(refusal)
    except BlockAction:
        raise
    except Exception as e:
        log("branch_guard check failed", {"error": str(e)})
        print(f"WARNING: branch_guard check failed ({e}) — guard bypassed", file=sys.stderr, flush=True)


def _fit_pretool(blocks: list[tuple[str, str]]) -> str:
    from hooks.context.injection_budget import budget_chars, fit
    from hooks.targets import emitter

    if emitter.forced():
        blocks = emitter.drain_blocks() + blocks
    return fit(blocks, budget_chars(), sep="\n\n")


def on_pre_tool_use(payload: dict) -> None:
    """Handle PreToolUse event.

    Conditions run first, so every guard judges the input that will run. Bypass mode lifts the block on
    secrets written to files, but detection, logging and telemetry still run; inline secrets that write no
    file are noted, never blocked. The credential read guard runs on every target and blocks rather than
    redacts, because a value that reaches the transcript must be rotated; bypass mode does not lift it.
    Broadcast and enforcement context merge into one hookSpecificOutput object, because Claude Code drops
    a second JSON line. Codex PreToolUse has no context channel, so enforcement is recovered on
    PostToolUse and broadcasts use their UserPromptSubmit path.
    The critical on PreToolUse broadcast opt in is owned by the broadcast module; this handler checks only
    BROADCAST_ENABLED.
    """
    from hooks.config import SECRETS_MODE

    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input", {})

    _gate_block = None
    try:
        from hooks.context.conditions import write_guard

        _gate_block = write_guard(tool_name, tool_input, payload.get("session_id", ""), payload.get("cwd"))
    except Exception as e:
        log("conditions write guard failed", {"error": str(e)})
    if _gate_block:
        raise BlockAction(_gate_block)

    _conditions = None
    try:
        from hooks.context.conditions import pre_effect

        _conditions = pre_effect(payload)
    except Exception as e:
        log("conditions pre-tool failed", {"error": str(e)})
    if _conditions is not None:
        from hooks.common import inject_context

        for _ctx in _conditions.contexts:
            inject_context(_ctx, also_log=False, skip_compression=True, kind="condition")
        if _conditions.block:
            raise BlockAction(_conditions.block)
        if _conditions.rewrite is not None:
            payload["tool_input"] = tool_input = _conditions.rewrite

    _quota_policy_ctx = None
    _quota_policy_block = None
    try:
        from hooks.config import QUOTA_POLICY_ENABLED

        if QUOTA_POLICY_ENABLED:
            from hooks.context.quota_policy import pretool as _quota_policy_pretool

            _quota_policy_block, _quota_policy_ctx = _quota_policy_pretool(
                payload.get("session_id", ""), tool_name, payload.get("cwd", "")
            )
    except Exception as e:
        log("quota policy pre-tool failed", {"error": str(e)})
    if _quota_policy_block:
        raise BlockAction(_quota_policy_block)

    _question_block = _operator_question(payload)
    if _question_block:
        raise BlockAction(_question_block)
    _operator_reminder(payload)

    try:
        from scripts.swarm.health.activity import record as _record_swarm_activity

        _record_swarm_activity(tool_name, tool_input, call_id=payload.get("tool_use_id") or "")
    except Exception as e:
        log("swarm activity record failed", {"error": str(e)})
    _swarm_heartbeat("working", payload=payload)

    from hooks.context.swarm_gates import pretool as _swarm_gates

    _swarm_block = _swarm_gates(tool_name, tool_input, payload.get("session_id", ""))
    if _swarm_block:
        raise BlockAction(_swarm_block)

    from hooks.lifecycle.guard import pretool as _lifecycle_pretool

    _lifecycle_block = _lifecycle_pretool(payload)
    if _lifecycle_block:
        raise BlockAction(_lifecycle_block)

    if SECRETS_MODE == "off":
        log(
            f"Pre tool use: {tool_name} (secrets scanning skipped, mode=off)",
            {"tool": tool_name, "session_id": payload.get("session_id", "")},
        )
    else:
        from hooks.secrets import iter_strings, redact, scan

        try:
            from hooks.context.controls_toggle import is_controls_disabled

            _secrets_bypass = is_controls_disabled(payload.get("session_id", ""))
        except Exception:
            _secrets_bypass = False

        if tool_name == "Bash":
            command = tool_input.get("command", "")
            safe_command = redact(command, mode=SECRETS_MODE)[:500]
            log(
                f"Pre tool use: {tool_name}",
                {"tool": tool_name, "command": safe_command, "session_id": payload.get("session_id", "")},
            )
            hits = scan(command, mode=SECRETS_MODE)
            if hits:
                names = ", ".join(hits)
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
                from hooks.common import inject_context

                inject_context(
                    f"NOTE: Secret(s) detected inline in Bash command ({names}). "
                    "Logged and noted — transcript secrecy is operator-managed. "
                    "Do not echo values back or persist them."
                )
        elif tool_name in ("Write", "Edit") or tool_name.startswith("mcp__"):
            if tool_name.startswith("mcp__"):
                content = "\n".join(iter_strings(tool_input))
            else:
                content = tool_input.get("content", "") or tool_input.get("new_string", "")
            log(f"Pre tool use: {tool_name}", {"tool": tool_name, "session_id": payload.get("session_id", "")})
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

    if tool_name in ("Edit", "Write"):
        try:
            from hooks.context.version_guard import check_version_guard

            check_version_guard(payload)
        except BlockAction:
            raise
        except Exception as e:
            log("version_guard check failed", {"error": str(e)})

    _check_shell_guards(payload)

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

    if tool_name in ("Bash", "Edit", "MultiEdit", "Write"):
        try:
            from hooks.context.serena_edit_guard import check as check_serena_edit

            check_serena_edit(payload)
        except BlockAction:
            raise
        except Exception as e:
            log("serena_edit_guard check failed", {"error": str(e)})
            print(
                f"WARNING: serena_edit_guard check failed ({e}) — guard bypassed",
                file=sys.stderr,
                flush=True,
            )

    if tool_name == "Bash":
        try:
            from hooks.context.dep_banner import check_dep_install

            check_dep_install(payload)
        except Exception as e:
            log("dep_banner check failed", {"error": str(e)})

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

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")

    if session_id and transcript_path:
        from hooks.observability.transcript import log_new_entries

        log_new_entries(session_id, transcript_path)

    _credential_rewrite = _credential_guard(payload, tool_name)
    _plan_read_guard(payload, tool_name)

    if tool_name == "Read":
        try:
            from hooks.config import FILE_READ_CACHE_ENABLED

            if FILE_READ_CACHE_ENABLED:
                from hooks.context.file_read_cache import check_and_block_redundant_read

                check_and_block_redundant_read(payload)
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

    from hooks.tool_memory import inject_memory

    inject_memory(tool_name=tool_name, session_id=session_id)

    from hooks.config import RETRY_BREAKER_ENABLED

    if RETRY_BREAKER_ENABLED:
        try:
            from hooks.context.retry_breaker import check_hard_block

            check_hard_block(payload)
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

    from hooks.config import (
        BRAIN_ENABLED,
        BROADCAST_ENABLED,
        ENFORCEMENT_INJECTION_ENABLED,
        QUOTA_USAGE_INJECTION_ENABLED,
        QUOTA_USAGE_TOOL_CALLS,
    )
    from hooks.targets.capabilities import can_inject_context

    _pretool_blocks: list[tuple[str, str]] = []
    _can_inject_pretool = can_inject_context("PreToolUse")
    _recycle_ctx = None
    try:
        from hooks.context.context_recycle import directive as _recycle_directive

        if _can_inject_pretool:
            _recycle_ctx = _recycle_directive(payload.get("session_id", ""))
    except Exception as e:
        log("context recycle pre-tool failed", {"error": str(e)})
    _tool_call_count = 0
    if BRAIN_ENABLED or ENFORCEMENT_INJECTION_ENABLED or QUOTA_USAGE_INJECTION_ENABLED:
        try:
            from hooks.context.enforcement import increment_and_get_count

            _tool_call_count = increment_and_get_count(session_id)
        except Exception as e:
            log("tool-call counter failed", {"error": str(e)})

    if _recycle_ctx:
        _pretool_blocks.append(("alert", _recycle_ctx))
    if _quota_policy_ctx:
        _pretool_blocks.append(("alert", _quota_policy_ctx))
    elif QUOTA_USAGE_INJECTION_ENABLED and _tool_call_count % max(1, QUOTA_USAGE_TOOL_CALLS) == 0:
        try:
            from hooks.context.quota_usage import quota_banner

            _quota_banner = quota_banner(session_id)
            if _quota_banner:
                _pretool_blocks.append(("other", _quota_banner))
        except Exception as e:
            log("quota usage pretool failed", {"error": str(e)})

    if BRAIN_ENABLED and _tool_call_count:
        try:
            from hooks.context.brain_adapter import maybe_refresh_on_tool_call

            _brain_ctx = maybe_refresh_on_tool_call(
                session_id,
                _tool_call_count,
                claim_delivery=_can_inject_pretool,
            )
            if _brain_ctx:
                _pretool_blocks.append(("brain", _brain_ctx))
        except Exception as e:
            log("brain_adapter tool-call refresh failed", {"error": str(e)})

    if BROADCAST_ENABLED:
        try:
            from hooks.context.broadcast import context_kind, get_pretool_context

            _broadcast_ctx = get_pretool_context(session_id, claim_unseen=_can_inject_pretool)
            if _broadcast_ctx:
                _pretool_blocks.append((context_kind(_broadcast_ctx), _broadcast_ctx))
        except Exception as e:
            log("broadcast pretool failed", {"error": str(e)})

    if _can_inject_pretool:
        _pretool_blocks.extend(("inbox", b) for b in _inbox_blocks(session_id, payload.get("cwd", "")))
        _pretool_blocks.extend(("refocus", b) for b in _refocus_blocks(session_id, "tool"))
        _pretool_blocks.extend(("refocus", b) for b in _wait_nudge_blocks(payload))

    if ENFORCEMENT_INJECTION_ENABLED:
        try:
            from hooks.context.enforcement import get_pretool_enforcements

            _enf_ctx = get_pretool_enforcements(
                session_id,
                payload.get("cwd", ""),
                claim_unseen=_can_inject_pretool,
                tool_call_count=_tool_call_count,
                tool_name=tool_name,
                tool_input=payload.get("tool_input") or {},
            )
            if _enf_ctx:
                _pretool_blocks.append(("enforcement", _enf_ctx))
        except Exception as e:
            log("enforcement pretool failed", {"error": str(e)})

    from hooks.targets import emitter as _emitter

    _pretool_context = _fit_pretool(_pretool_blocks)

    _cond_decision = _conditions.decision if _conditions is not None else None
    if _credential_rewrite or _cond_decision in ("allow", "ask"):
        _updated, _note = _credential_rewrite or ({}, "")
        _rewritten = bool(_credential_rewrite) or (_conditions is not None and _conditions.rewrite is not None)
        emit_permission_decision(
            "PreToolUse",
            "ask" if _cond_decision == "ask" else "allow",
            "\n".join(r for r in (_note, _conditions.reason if _conditions is not None else "") if r),
            updated_input={**(payload.get("tool_input") or {}), **_updated} if _rewritten else None,
            additional_context=_pretool_context or None,
        )
    elif _pretool_context:
        if _can_inject_pretool and not _emitter.forced():
            _emitter.buffer_context(_pretool_context)
        elif _can_inject_pretool:
            import json as _json

            print(
                _json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "additionalContext": _pretool_context,
                        }
                    }
                )
            )
        else:
            log(
                "pretool context dropped — no PreToolUse context channel on this target",
                {"blocks": len(_pretool_blocks)},
            )


_NEAR_CREDENTIAL = re.compile(
    r"\.env\b|\.env\.|\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.pgpass"
    r"|\.bashrc|\.bash_profile|\.bash_login|\.profile|\.zshrc|\.zprofile|\.zshenv"
    r"|\.bash_history|\.zsh_history|\.aws/credentials"
)


def _near_credential(tool_input: dict) -> bool:
    """Whether a tool input names a credential path — decides fail-closed vs fail-open."""
    try:
        from hooks.context.credential_guard import near_credential

        return near_credential(tool_input)
    except Exception:  # NOSONAR
        return bool(_NEAR_CREDENTIAL.search(json.dumps(tool_input, default=str)))


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
    _operator_words(payload)
    _swarm_after_tool(payload)
    _conditions = None
    try:
        from hooks.context.conditions import post_effect

        _conditions = post_effect(payload)
        if _conditions is not None:
            from hooks.common import inject_context
            from hooks.targets import emitter

            for _ctx in _conditions.contexts:
                inject_context(_ctx, also_log=False, skip_compression=True, kind="condition")
            emitter.set_fields(_conditions.hook_fields, _conditions.top_fields)
    except Exception as e:
        log("conditions post-tool failed", {"error": str(e)})

    try:
        from hooks.config import ENFORCEMENT_INJECTION_ENABLED
        from hooks.targets.capabilities import can_inject_context

        if not can_inject_context("PreToolUse"):
            from hooks.common import inject_context
            from hooks.config import BROADCAST_ENABLED
            from hooks.context.context_recycle import directive as _recycle_directive

            recycle_context = _recycle_directive(_trace_session_id)
            if recycle_context:
                inject_context(recycle_context, also_log=False, skip_compression=True)
            from hooks.context.task_context import deliver_pending

            project_context = deliver_pending(_trace_session_id, "PostToolUse")
            if project_context:
                inject_context(project_context, also_log=False, skip_compression=True)
            if BROADCAST_ENABLED:
                from hooks.context.broadcast import get_posttool_context

                broadcast_context = get_posttool_context(_trace_session_id)
                if broadcast_context:
                    inject_context(broadcast_context, also_log=False, skip_compression=True)
            if ENFORCEMENT_INJECTION_ENABLED:
                from hooks.context.enforcement import get_posttool_enforcements

                enforcement_context = get_posttool_enforcements(
                    _trace_session_id,
                    payload.get("cwd", ""),
                    tool_name=tool_name,
                    tool_input=payload.get("tool_input") or {},
                )
                if enforcement_context:
                    inject_context(enforcement_context, also_log=False, skip_compression=True)
            _inject_after_tool(payload)
    except Exception as e:
        log("enforcement posttool fallback failed", {"error": str(e)})

    _inject_ledger_decision(payload)

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
        try:
            from scripts.gates.placement import heard as placement_heard

            placement_heard(payload)
        except Exception as e:
            log("placement answer record failed", {"error": str(e)})

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    if session_id and transcript_path:
        _trace_mark("transcript_logger", tool_name, _trace_session_id)
        from hooks.observability.transcript import log_new_entries

        log_new_entries(session_id, transcript_path)

    if tool_name == "Bash":
        _trace_mark("bash_output_filter", tool_name, _trace_session_id)
        try:
            from hooks.config import BASH_FILTER_ENABLED

            if BASH_FILTER_ENABLED:
                from hooks.context.bash_output_filter import filter_bash_output

                _response = payload.get("tool_response")
                filtered = filter_bash_output(
                    tool_name,
                    payload.get("tool_input", {}),
                    payload.get("tool_output")
                    or (str(_response.get("stdout") or "") if isinstance(_response, dict) else ""),
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
                    from hooks.targets import buffers_single_envelope, emitter
                    from hooks.targets.capabilities import supports_output_rewrite

                    if supports_output_rewrite() and isinstance(_response, dict):
                        if _conditions is None or "updatedToolOutput" not in _conditions.hook_fields:
                            emitter.set_fields({"updatedToolOutput": {**_response, "stdout": filtered}})
                    elif buffers_single_envelope() or emitter.forced():
                        emitter.buffer_context(filtered)
                    else:
                        print(_json.dumps({"additionalContext": filtered}))
        except Exception as e:
            log("bash_output_filter failed", {"error": str(e)})

    if tool_name == "Read":
        _trace_mark("file_read_cache_mark", tool_name, _trace_session_id)
        try:
            from hooks.config import FILE_READ_CACHE_ENABLED

            if FILE_READ_CACHE_ENABLED:
                from hooks.context.file_read_cache import mark_read_result

                mark_read_result(payload)
        except Exception as e:
            log("file_read_cache mark failed", {"error": str(e)})

    _trace_mark("tool_memory_record_error", tool_name, _trace_session_id)
    from hooks.tool_memory import record_error

    record_error(payload)

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

    from hooks.config import RETRY_BREAKER_ENABLED

    if RETRY_BREAKER_ENABLED:
        _trace_mark("retry_breaker", tool_name, _trace_session_id)
        try:
            from hooks.context.retry_breaker import on_post_tool_result

            on_post_tool_result(payload)
        except Exception as e:
            log("retry_breaker post-tool failed", {"error": str(e)})

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


def _link_codex_rollout(transcript_path: str) -> None:
    from hooks.targets import is_codex

    if not is_codex():
        return
    try:
        from scripts import codex_rollouts

        codex_rollouts.link_current(transcript_path)
    except Exception as e:
        log("codex rollout link failed", {"error": str(e)})


def on_stop(payload: dict) -> None:
    """Handle Stop event.

    Marker publishing, error mail, transcript logging and memory save run forked, because each can take
    seconds and must never block session cleanup. Only per turn signals clear here; session scoped signals
    persist until SessionEnd.
    """
    _stop_block = None
    try:
        from hooks.context import conditions, operator_mode

        _stop_block = conditions.stop_block(payload) or operator_mode.reply_refusal(payload) or None
    except Exception as e:
        log("conditions stop failed", {"error": str(e)})
    if _stop_block is not None:
        raise BlockAction(_stop_block)

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    _confirm_inbox(session_id)
    _link_codex_rollout(transcript_path)

    metrics = parse_transcript_metrics(transcript_path) if transcript_path else {}
    _swarm_heartbeat("idle", payload=payload)
    _master_login_checkpoint()
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

    _request_trace_flush(payload, "stop")

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

    try:
        from hooks.context.branch_guard import clear_branch_signal
        from hooks.context.conditions import disarm_gate
        from hooks.context.prod_lockdown import clear_bypass

        clear_bypass(session_id)
        clear_branch_signal(session_id)
        disarm_gate(session_id)
    except Exception as e:
        log("prod_lockdown.clear_bypass failed", {"error": str(e)})

    from hooks.tool_memory import scan_transcript

    scan_transcript(payload)

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

    try:
        from hooks.config import VOICE_ENABLED

        if VOICE_ENABLED:
            from hooks.context.voice_output import maybe_speak

            maybe_speak(session_id, payload.get("last_assistant_message", ""))
    except Exception as e:
        log("voice_output stop hook failed", {"error": str(e)})
    _swarm_ledger(payload)

    try:
        from hooks.lifecycle.refresh import on_stop as refresh_on_stop

        if refresh_on_stop(payload):
            log("session refresh scheduled", {"session_id": session_id})
    except Exception as e:
        log("session refresh failed", {"error": str(e)})

    try:
        from hooks.lifecycle.handoff_close import on_stop as handoff_close_on_stop

        if handoff_close_on_stop(payload):
            log("handed-off session close scheduled", {"session_id": session_id})
    except Exception as e:
        log("handed-off session close failed", {"error": str(e)})


def on_subagent_start(payload: dict) -> None:
    """Handle SubagentStart event — wire the brain through to subagents.

    Mirrors on_session_start brain logic so dispatched subagents get:
      - brain context injected via brain_adapter
      - broadcasts delivered via broadcast.check_and_inject_broadcasts
      - brain.inject + brain.delivery spans visible in ClickHouse/Langfuse

    This is the fix for the "subagents have no brain" gap (BRAIN-MVP item #21).

    Each subagent session id is recorded in _KNOWN_SUBAGENT_IDS, so a subagent prompt never arms a
    release, hotfix, push or PR signal for itself.
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

    try:
        from hooks.config import CI_MANIFESTO_ENABLED

        if CI_MANIFESTO_ENABLED:
            from hooks.context.ci_manifesto import (
                inject_on_session_start as inject_ci_manifesto,
            )

            inject_ci_manifesto()
    except Exception as e:
        log("ci_manifesto subagent_start failed", {"error": str(e)})

    try:
        from hooks.config import BRAIN_ENABLED

        if BRAIN_ENABLED:
            from hooks.context.brain_adapter import inject_on_session_start

            inject_on_session_start(agent_id, payload.get("cwd", ""))
    except Exception as e:
        log("brain_adapter subagent_start failed", {"error": str(e)})

    try:
        from hooks.config import BROADCAST_ENABLED

        if BROADCAST_ENABLED and agent_id:
            from hooks.context.account_sessions import agent_pid, session_account
            from hooks.context.broadcast import (
                check_and_inject_broadcasts,
                register_session,
            )

            _agent_pid = agent_pid()
            register_session(
                agent_id,
                pid=_agent_pid,
                cwd=payload.get("cwd", ""),
                model=payload.get("model", ""),
                account=session_account(_agent_pid),
                supersede=False,
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

    try:
        from hooks.context.branch_guard import clear_branch_signal, clear_pr_signal
        from hooks.context.prod_lockdown import clear_bypass, clear_release_signal

        clear_bypass(agent_id)
        clear_release_signal(agent_id)
        clear_branch_signal(agent_id)
        clear_pr_signal(agent_id)
    except Exception:
        pass

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
    try:
        from scripts.swarm import remote_control

        remote_control.on_notification(payload)
    except Exception as e:
        log("remote control notice failed", {"error": str(e)})


def on_pre_compact(payload: dict) -> None:
    """Handle PreCompact event."""
    session_id = payload.get("session_id", "")
    log("Pre compact", {"session_id": session_id})
    try:
        from hooks.context.swarm_refocus import mark_compacted

        mark_compacted(session_id)
    except Exception as e:
        log("swarm refocus compact mark failed", {"error": str(e)})
    _request_trace_flush(payload, "compact")


def _request_trace_flush(payload: dict, reason: str) -> None:
    try:
        from hooks.observability import trace_flush

        trace_flush.request(payload.get("session_id", ""), payload.get("transcript_path", ""), reason)
    except Exception as e:
        log("trace flush request failed", {"error": str(e)})


def on_permission_request(payload: dict) -> None:
    """Handle PermissionRequest event."""
    tool_name = payload.get("tool_name", "unknown")
    log(f"Permission requested: {tool_name}", {"tool": tool_name})
    from hooks.context import master_coding, operator_mode, planner_plan

    if planner_plan.accepts(tool_name):
        print(json.dumps(planner_plan.grant(payload.get("tool_input") or {})))
    elif master_coding.asks_operator(tool_name, payload.get("session_id")):
        return
    elif tool_name in master_coding.PERMISSION_TOOLS:
        emit_permission_decision("PermissionRequest", "deny", operator_mode.PROMPT_REFUSAL.format(tool=tool_name))
    elif os.environ.get("AGENTIHOOKS_SWARM"):
        emit_permission_decision(
            "PermissionRequest",
            "deny",
            f"Denied {tool_name}: swarm sessions and subagents cannot wait for permission. "
            "Use an allowed tool or report the missing capability through the swarm inbox.",
        )


def emit_permission_decision(
    event_name: str,
    decision: str,
    reason: str = "",
    *,
    updated_input: dict | None = None,
    additional_context: str | None = None,
) -> None:
    """Print a ``hookSpecificOutput.permissionDecision`` envelope.

    The single legal call site for emitting a permission decision — route any
    future allow/deny/ask logic through here rather than printing directly.
    *decision* is filtered through ``hooks.targets.capabilities.
    allowed_permission_decisions()`` for the current target first: codex's
    PreToolUse output supports ``"deny"`` only (no allow/ask), so a decision
    outside the target's allowed set is dropped with a log line instead of
    being printed. This is what structurally closes the codex path even
    though nothing emits a non-deny decision today — a future "allow"
    fast-path can't leak through it.

    An allow whose rewrite the host cannot apply would approve the original call, so the whole decision is
    dropped. Copilot command hooks deny with a top level decision block object, the only shape that blocks
    userPromptSubmitted; hookSpecificOutput is not emitted for Copilot because its command transport
    ignores it.
    """
    from hooks.targets import current_target
    from hooks.targets.capabilities import allowed_permission_decisions, arg_mutation_field, supports_arg_mutation

    target = current_target()
    if decision not in allowed_permission_decisions(target):
        log(
            "permission decision dropped — not allowed on this target",
            {"target": target, "event": event_name, "decision": decision},
        )
        return
    if updated_input is not None and not supports_arg_mutation(target):
        log(
            "permission decision dropped — target cannot mutate tool input",
            {"target": target, "event": event_name, "decision": decision},
        )
        return

    if target == "copilot":
        if decision != "deny":
            log(
                "permission decision dropped — copilot has no channel for it",
                {"target": target, "event": event_name, "decision": decision},
            )
            return
        output: dict[str, Any] = {"decision": "block"}
        if reason:
            output["reason"] = reason
        print(json.dumps(output))
        return

    if target == "claude" and event_name == "PermissionRequest":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": event_name,
                        "decision": {"behavior": decision, "message": reason},
                    }
                }
            )
        )
        return

    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "permissionDecision": decision,
        }
    }
    if reason:
        output["hookSpecificOutput"]["permissionDecisionReason"] = reason
    if updated_input is not None:
        output["hookSpecificOutput"][arg_mutation_field(target)] = updated_input
    if additional_context:
        output["hookSpecificOutput"]["additionalContext"] = additional_context
    print(json.dumps(output))


_KNOWN_SUBAGENT_IDS: set[str] = set()
_EVENT_STARTED: float | None = None

EVENT_HANDLERS = {
    "SessionStart": on_session_start,
    "SessionEnd": on_session_end,
    "UserPromptSubmit": on_user_prompt_submit,
    "PreToolUse": lambda payload: tool_end.closing_denied(on_pre_tool_use, payload, BlockAction, _swarm_tool_end),
    "PostToolUse": on_post_tool_use,
    "Stop": on_stop,
    "SubagentStart": on_subagent_start,
    "SubagentStop": on_subagent_stop,
    "Notification": on_notification,
    "PreCompact": on_pre_compact,
    "PermissionRequest": on_permission_request,
    "PostToolUseFailure": _swarm_tool_end,
}


@skip_classifier_child
def main() -> None:
    """Main entry point - routes events to handlers.

    Uses ``os._exit`` instead of ``sys.exit`` to bypass Python's shutdown
    sequence. This is critical: non-daemon OTEL exporter / BatchProcessor
    threads would otherwise block process exit for 10-20s while they drain
    retries against an unreachable collector. The hook's output is already
    flushed by the handlers (stderr for banners, log() files), so skipping
    atexit handlers is safe.

    Parse failures fail open, except a garbled payload that names a tool permission event, which is
    denied, because exiting 0 reads as allowed where the tool gate fails closed on non zero. Only the head
    of the payload is searched for that event name, so a marker embedded deep in a garbled non tool event
    cannot deny it. On Copilot the event comes from AGENTIHOOKS_COPILOT_EVENT, trusted only on the Copilot
    target, since a nested hook can inherit it. A block from any call in a Copilot batch denies the whole
    batch. Codex and Copilot parse stdout as one JSON object, so injected context is buffered and flushed
    once; on a block that buffered context is folded into the stderr message, and no exit path leaves
    context buffered for the next event.

    The blocked event name is bound before the try, so the outer BlockAction handler can name the event
    even when the block fires before dispatch.
    """
    _blocked_event = "Unknown"
    try:
        _raw_stdin = sys.stdin.read()
        payload: dict[str, Any] = json.loads(_raw_stdin)

        from hooks.targets.normalizer import normalize_payload

        payload = normalize_payload(payload)

        from hooks.observability.token_monitor import record_lifecycle_context

        record_lifecycle_context(payload)

        event_name = payload.get("hook_event_name", "Unknown")
        _blocked_event = event_name

        from hooks.context.swarm_pin import unpin

        unpin()

        from hooks.context.claim_fence import enforce as claim_fence

        claim_fence(payload)

        handler = EVENT_HANDLERS.get(event_name)

        from hooks.targets import buffers_single_envelope, emitter
        from hooks.targets.capabilities import supports_output_rewrite

        if event_name in ("PreToolUse", "SessionStart", "UserPromptSubmit") and not buffers_single_envelope():
            emitter.force_buffer(plain=event_name != "PreToolUse")
        elif event_name == "PostToolUse" and supports_output_rewrite():
            emitter.force_buffer()

        try:
            if handler:
                handler(payload)
                for _call in payload.get("copilot_extra_tool_calls") or []:
                    handler({**payload, **_call})
            else:
                log(f"Unknown event: {event_name}", payload)

            emitter.flush(event_name)

        except BlockAction as e:
            reason = str(e)
            if buffers_single_envelope() or emitter.forced():
                drained = emitter.drain_for_block()
                if drained:
                    reason = f"{reason}\n\n{drained}"
            raise BlockAction(reason) from e
        finally:
            emitter.drain()

    except BlockAction as e:
        try:
            from hooks.targets.capabilities import requires_envelope_block

            if requires_envelope_block(_blocked_event):
                emit_permission_decision(_blocked_event, "deny", str(e))
        except Exception:  # NOSONAR
            pass
        print(str(e), file=sys.stderr, flush=True)
        otel.flush()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(2)
    except json.JSONDecodeError:
        import re as _re

        from hooks.targets import is_copilot

        _env_event = os.environ.get("AGENTIHOOKS_COPILOT_EVENT", "") if is_copilot() else ""
        if _env_event in ("preToolUse", "preMcpToolCall", "permissionRequest") or _re.search(
            r'"(?:hook_?[eE]vent_?[nN]ame|hook_?[tT]ype)"\s*:\s*"(?:[pP]reToolUse|'
            r'[pP]reMcpToolCall|[pP]ermissionRequest)"',
            _raw_stdin[:512],
        ):
            log("Unparseable payload names a tool-permission event — denying", {"bytes": len(_raw_stdin)})
            print(
                "BLOCKED: hook payload was not valid JSON but names a tool-permission "
                "event — denying rather than silently allowing an unscanned tool call.",
                file=sys.stderr,
                flush=True,
            )
            os._exit(2)
        log("Failed to parse JSON payload")
    except KeyboardInterrupt:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    except Exception as e:
        log(f"Hook manager error: {str(e)}")
        otel.flush()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

    otel.flush()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
