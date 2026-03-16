"""Bash output filter — truncates verbose command output to reduce context tokens.

Detection priority (checked in order):
1. docker_logs  — docker logs / docker compose logs
2. kubectl      — kubectl commands
3. git_log      — git log
4. test_runner  — pytest/jest/npm test/cargo test output
5. build_output — npm install / pip install / cargo build
6. generic      — fallback hard cap

Only fires for tool_name == "Bash".  Returns None when output is already
within limits (no unnecessary modification).
"""

import re
from typing import Optional

from hooks.config import (
    BASH_FILTER_GIT_MAX_COMMITS,
    BASH_FILTER_MAX_CHARS,
    BASH_FILTER_MAX_LINES,
    BASH_FILTER_TEST_MAX_FAILURES,
)


# ---------------------------------------------------------------------------
# Truncation helpers
# ---------------------------------------------------------------------------


def truncate_docker_logs(output: str, max_lines: int = BASH_FILTER_MAX_LINES) -> str:
    """Keep the last *max_lines* lines; prepend truncation notice."""
    lines = output.splitlines()
    if len(lines) <= max_lines:
        return output
    kept = lines[-max_lines:]
    notice = f"[truncated: kept last {max_lines} of {len(lines)} lines]"
    return notice + "\n" + "\n".join(kept)


def truncate_test_output(output: str, max_failures: int = BASH_FILTER_TEST_MAX_FAILURES) -> str:
    """Strip PASSED lines; keep summary block + first *max_failures* FAILED blocks."""
    lines = output.splitlines()

    # Identify summary block (last section after a blank line or "==" separator)
    summary_start = 0
    for i in range(len(lines) - 1, -1, -1):
        if re.match(r"^[=\-]{5,}", lines[i]) or (lines[i].strip() == "" and i > len(lines) - 20):
            summary_start = i
            break

    summary_lines = lines[summary_start:]
    body_lines = lines[:summary_start]

    # Remove PASSED lines from body
    filtered_body = [l for l in body_lines if not re.search(r"\bPASSED\b", l)]

    # Extract FAILED blocks (split on "FAILED " or "FAIL " markers)
    failure_blocks: list[list[str]] = []
    current_block: list[str] = []
    in_failure = False
    for line in filtered_body:
        if re.search(r"^(FAILED|FAIL )", line):
            if current_block:
                failure_blocks.append(current_block)
            current_block = [line]
            in_failure = True
        elif in_failure:
            if line.strip() == "" or re.match(r"^[=\-]{5,}", line):
                if current_block:
                    failure_blocks.append(current_block)
                current_block = []
                in_failure = False
            else:
                current_block.append(line)
        else:
            current_block.append(line)

    if current_block:
        failure_blocks.append(current_block)

    kept_failures = failure_blocks[:max_failures]
    truncated_count = len(failure_blocks) - len(kept_failures)

    result_parts: list[str] = []
    for block in kept_failures:
        result_parts.extend(block)

    if truncated_count > 0:
        result_parts.append(f"[truncated: {truncated_count} additional failure(s) omitted]")

    result_parts.extend(summary_lines)
    return "\n".join(result_parts)


def truncate_git_log(output: str, max_commits: int = BASH_FILTER_GIT_MAX_COMMITS) -> str:
    """Keep first *max_commits* commits (split on 'commit ' boundaries)."""
    # Split on commit SHA boundaries
    parts = re.split(r"(?=^commit [0-9a-f]{7,40})", output, flags=re.MULTILINE)
    # Filter empty leading parts
    parts = [p for p in parts if p.strip()]

    if len(parts) <= max_commits:
        return output

    kept = parts[:max_commits]
    notice = f"\n[truncated: kept {max_commits} of {len(parts)} commits]"
    return "".join(kept) + notice


def truncate_generic(output: str, max_chars: int = BASH_FILTER_MAX_CHARS) -> str:
    """Hard character cap with truncation notice."""
    if len(output) <= max_chars:
        return output
    trimmed = output[:max_chars]
    notice = f"\n[...truncated {len(output) - max_chars} chars...]"
    return trimmed + notice


# ---------------------------------------------------------------------------
# Output type detection
# ---------------------------------------------------------------------------


def _detect_output_type(command: str, output: str) -> str:
    """Return the detected output category string."""
    cmd_lower = command.lower()

    if "docker logs" in cmd_lower or "docker compose logs" in cmd_lower:
        return "docker_logs"
    if "kubectl" in cmd_lower:
        return "kubectl"
    if "git log" in cmd_lower:
        return "git_log"

    # Test runner detection by output patterns
    if re.search(r"(PASSED|FAILED|ERROR|pytest|jest|npm test|cargo test)", output, re.IGNORECASE):
        return "test_runner"

    if re.search(r"(npm install|pip install|cargo build)", cmd_lower):
        return "build_output"

    return "generic"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def filter_bash_output(
    tool_name: str,
    tool_input: dict,
    tool_output: str,
) -> Optional[str]:
    """Filter bash output; returns filtered string or None (no change needed).

    Returns None when:
    - tool_name is not "Bash"
    - output is already within all limits
    """
    if tool_name != "Bash":
        return None

    if not tool_output:
        return None

    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

    output_type = _detect_output_type(command, tool_output)

    if output_type == "docker_logs":
        lines = tool_output.splitlines()
        if len(lines) <= BASH_FILTER_MAX_LINES:
            return None
        return truncate_docker_logs(tool_output)

    if output_type == "kubectl":
        lines = tool_output.splitlines()
        if len(lines) <= BASH_FILTER_MAX_LINES:
            return None
        return truncate_docker_logs(tool_output)  # same behaviour: keep last N lines

    if output_type == "git_log":
        parts = re.split(r"(?=^commit [0-9a-f]{7,40})", tool_output, flags=re.MULTILINE)
        parts = [p for p in parts if p.strip()]
        if len(parts) <= BASH_FILTER_GIT_MAX_COMMITS:
            return None
        return truncate_git_log(tool_output)

    if output_type == "test_runner":
        if len(tool_output) <= BASH_FILTER_MAX_CHARS:
            return None
        return truncate_test_output(tool_output)

    # build_output and generic both use the hard char cap
    if len(tool_output) <= BASH_FILTER_MAX_CHARS:
        return None
    return truncate_generic(tool_output)
