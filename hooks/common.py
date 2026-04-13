"""Common utilities for hook scripts."""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from hooks.config import LOG_ENABLED, LOG_FILE, LOG_HOOKS_COMMANDS, LOG_USE_COLORS

__all__ = [
    # Logging (writes to log file - Claude does NOT see this)
    "log",
    "log_command",
    "log_transcript",
    "output_json",
    # Context injection (prints to STDOUT - Claude SEES this)
    "inject_context",
    "inject_file",
    "inject_banner",
    # Script runner
    "run_script",
    # AWS integration (lazy loaded)
    "AWSConfigParser",
    "AWSAccount",
    "get_aws_profiles",
    "get_aws_account_id",
    "get_all_aws_accounts",
    "find_aws_account",
    # Email integration (lazy loaded)
    "EmailClient",
    "EmailResult",
    "send_email",
    "send_markdown_file",
    "markdown_to_html",
    "wrap_html_body",
    "parse_recipients",
    # Metrics (lazy loaded)
    "Timer",
    "MetricsCollector",
    "ResultAccumulator",
    "timed",
    # Session correlation (for stateless sessions)
    "get_correlation_id",
    "get_session_context",
]


# AWS integration exports
_AWS_EXPORTS = {
    "AWSConfigParser",
    "AWSAccount",
    "get_aws_profiles",
    "get_aws_account_id",
    "get_all_aws_accounts",
    "find_aws_account",
}

# Email integration exports
_EMAIL_EXPORTS = {
    "EmailClient",
    "EmailResult",
    "send_email",
    "send_markdown_file",
    "markdown_to_html",
    "wrap_html_body",
    "parse_recipients",
}

# Metrics exports
_METRICS_EXPORTS = {
    "Timer",
    "MetricsCollector",
    "ResultAccumulator",
    "timed",
}


def __getattr__(name: str):
    """Lazy load integrations to avoid circular imports."""
    if name in _AWS_EXPORTS:
        from hooks.integrations import aws

        return getattr(aws, name)
    if name in _EMAIL_EXPORTS:
        from hooks.integrations import mailer

        return getattr(mailer, name)
    if name in _METRICS_EXPORTS:
        from hooks.observability import metrics

        return getattr(metrics, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# =============================================================================
# LOGGING
# =============================================================================


_BRAIN_LOG_PREFIXES = ("brain_", "broadcast_", "outbox_", "amygdala_")


def log(message: str, payload: dict | None = None) -> None:
    """Write log entry to file (JSON format), optionally fan out to OTLP logs."""
    if not LOG_ENABLED:
        return

    try:
        log_path = Path(LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        entry = {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
        }
        if payload:
            entry["payload"] = payload

        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent failure - never break Claude

    # OTLP log fan-out for brain-tagged events (deterministic audit trail).
    # Gated by OTEL_HOOK_LOG_FANOUT (inherits OTEL_HOOKS_ENABLED by default).
    try:
        if any(message.startswith(p) for p in _BRAIN_LOG_PREFIXES):
            from hooks.telemetry import emit_log

            emit_log(message, payload or {})
    except Exception:
        pass


def log_command(script_name: str, output: str) -> None:
    """Log command output in readable format (only if LOG_HOOKS_COMMANDS=true)."""
    if not LOG_ENABLED or not LOG_HOOKS_COMMANDS:
        return

    try:
        log_path = Path(LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).isoformat()
        separator = "=" * 80

        log_entry = f"""
{separator}
[@timestamp] {timestamp}
[script] {script_name}
{separator}
{output}
{separator}
"""
        with open(log_path, "a") as f:
            f.write(log_entry)
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent failure - never break Claude


def log_transcript(conversation_id: str, entry_type: str, content: str) -> None:
    """Log transcript entry in readable format."""
    if not LOG_ENABLED:
        return

    try:
        log_path = Path(LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).isoformat()
        separator = "-" * 80
        type_icon = "👤" if entry_type == "user" else "🤖"

        # ANSI colors (disabled for CloudWatch compatibility)
        if LOG_USE_COLORS:
            YELLOW = "\033[93m"
            GREEN = "\033[92m"
            RESET = "\033[0m"
        else:
            YELLOW = GREEN = RESET = ""

        log_entry = f"""
{separator}
{YELLOW}[@timestamp] {timestamp}{RESET}
{YELLOW}[conversation] {conversation_id}{RESET}
{GREEN}[{entry_type}] {type_icon}{RESET}
{separator}
{content}
{separator}
"""
        with open(log_path, "a") as f:
            f.write(log_entry)
    except Exception:  # NOSONAR — hooks must never crash the parent process
        pass  # Silent failure - never break Claude


def output_json(data: dict) -> None:
    """Output JSON response to stdout (for hook responses)."""
    print(json.dumps(data))


# =============================================================================
# CONTEXT INJECTION (Output to Claude's context window)
# =============================================================================
# IMPORTANT: These functions print to STDOUT, which gets captured by Claude Code
# and injected into the agent's context window. This is the ONLY way to make
# content visible to Claude during a session. The log() functions above write
# to log files which Claude does NOT see.


def inject_context(content: str, also_log: bool = True, skip_compression: bool = False) -> None:
    """Inject content into Claude's context window via STDOUT.

    This is the ONLY way to make content visible to Claude during a session.
    Hook STDOUT gets captured and injected into Claude's context.

    Args:
        content: The content to inject (will be printed to STDOUT)
        also_log: If True, also write to log file for debugging
        skip_compression: If True, bypass the preprocessor (used when caller already compressed)
    """
    # Apply token compression if scope=all and not already compressed
    if not skip_compression:
        try:
            from hooks.config import CONTEXT_COMPRESSION_SCOPE

            if CONTEXT_COMPRESSION_SCOPE == "all":
                from hooks.context.preprocessor import get_level_from_config, preprocess

                level = get_level_from_config()
                if level > 0:
                    content = preprocess(content, level)
        except Exception:
            pass

    # Print to STDOUT - this gets injected into Claude's context
    print("=== CONTEXT INJECTION ===")
    print(content)

    # Optionally also log the actual content for debugging (not just metadata)
    if also_log and LOG_ENABLED:
        log_command("context_injection", content)


def inject_file(file_path: str, also_log: bool = True) -> bool:
    """Read a file and inject its contents into Claude's context.

    Args:
        file_path: Path to the file to read
        also_log: If True, also write to log file for debugging

    Returns:
        True if successful, False if file not found or error
    """
    import sys

    try:
        path = Path(file_path)
        if not path.exists():
            print(f"[ERROR] File not found: {file_path}", file=sys.stderr)
            return False

        content = path.read_text(encoding="utf-8")
        inject_context(content, also_log=also_log)
        return True

    except Exception as e:
        print(f"[ERROR] Failed to read file: {e}", file=sys.stderr)
        return False


def inject_banner(title: str, content: str, also_log: bool = True, skip_compression: bool = False) -> None:
    """Inject a formatted banner into Claude's context.

    Creates a visible box around important information that Claude will see.

    Args:
        title: Banner title (displayed at top)
        content: Content to display in the banner body
        also_log: If True, also write to log file for debugging
        skip_compression: If True, bypass the preprocessor (used when caller already compressed)
    """
    width = 78
    border = "═" * width

    # Format title line
    title_line = f"║  {title}"
    title_line = title_line.ljust(width + 1) + "║"

    # Format content lines
    content_lines = []
    for line in content.strip().split("\n"):
        formatted = f"║  {line}"
        formatted = formatted.ljust(width + 1) + "║"
        content_lines.append(formatted)

    # Print 2 blank lines first (separate prints to avoid stripping)

    # Then print the banner
    banner = f"""
╔{border}╗
{title_line}
╠{border}╣
{chr(10).join(content_lines)}
╚{border}╝
    """

    inject_context(banner, also_log=also_log, skip_compression=skip_compression)


# =============================================================================
# SCRIPT RUNNER
# =============================================================================


def run_script(script_name: str, *args: str, timeout: int = 30) -> str:
    """
    Run a script from hooks/scripts/ and return its stdout.

    Args:
        script_name: Name of the script (e.g., 'command_context.sh')
        *args: Additional arguments to pass to the script
        timeout: Timeout in seconds (default: 30)

    Returns:
        stdout from the script, empty string on failure
    """
    scripts_dir = Path(__file__).parent / "scripts"
    script_path = scripts_dir / script_name

    if not script_path.exists():
        return f"Script not found: {script_name}"

    try:
        result = subprocess.run(
            ["bash", str(script_path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout

        # Log command output if enabled
        log_command(script_name, output)

        return output
    except subprocess.TimeoutExpired:
        return f"Script timed out after {timeout}s: {script_name}"
    except Exception as e:
        return f"Script error: {e}"


# =============================================================================
# SESSION CORRELATION (For stateless session support)
# =============================================================================
# When stateless=True is used in API calls, the agent generates a fresh Claude
# session ID but preserves the external UUID for correlation. These functions
# help hooks read the correlation context from environment variables.


def get_correlation_id(claude_session_id: Optional[str] = None) -> str:
    """Get the external correlation UUID for the current session.

    When stateless=True, the Claude session ID differs from the external UUID.
    This function returns the external UUID that hooks should use for:
    - State tracking in conversation_map.json
    - Platform-specific operations (Teams, Slack, Discord)
    - Correlating multiple Claude sessions back to one conversation

    Args:
        claude_session_id: Optional fallback if env var not set

    Returns:
        External correlation UUID (falls back to claude_session_id if not found)
    """
    return os.environ.get(
        "AGENTICORE_CORRELATION_ID", claude_session_id or os.environ.get("AGENTICORE_CLAUDE_SESSION_ID", "")
    )


def get_session_context() -> Dict[str, Optional[str]]:
    """Get full session context from environment.

    Returns a dict with both IDs for hooks that need to track both:
    - correlation_id: External UUID (for platform/state correlation)
    - claude_session_id: Claude's internal session ID

    When stateless=False, both IDs are the same.
    When stateless=True, they differ (claude_session_id is fresh UUID).

    Returns:
        Dict with correlation_id and claude_session_id
    """
    correlation_id = os.environ.get("AGENTICORE_CORRELATION_ID")
    claude_session_id = os.environ.get("AGENTICORE_CLAUDE_SESSION_ID")

    # Check if this is a stateless session (different IDs)
    is_stateless = correlation_id is not None and claude_session_id is not None and correlation_id != claude_session_id

    return {
        "correlation_id": correlation_id,
        "claude_session_id": claude_session_id,
        "is_stateless": is_stateless,
    }
