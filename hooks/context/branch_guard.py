"""Branch Guard — blocks destructive git operations on main/master.

Prevents merges into main/master, hard resets, force pushes, and git tagging.
Read-only operations (checkout, switch, pull) are allowed.
Normal pushes are allowed — branch protection is a remote concern.

Public API:
    check_branch_guard(payload)  — called from on_pre_tool_use for Bash
"""

import re

from hooks.common import log
from hooks.hook_manager import BlockAction

_BLOCKED_PATTERNS = [
    # Merge into main/master (direct merge bypasses PR workflow)
    (
        re.compile(r"git\s+merge\s+.*\b(main|master)\b"),
        "Direct merge into main/master is blocked. Create a PR instead.",
    ),
    # Reset main/master (destructive — rewrites history)
    (re.compile(r"git\s+reset\s+.*\b(main|master)\b"), "Resetting main/master is blocked — this rewrites history."),
    # Force push (any branch — can destroy remote history)
    (re.compile(r"git\s+push\s+--force"), "Force push is blocked — this can destroy remote history."),
    (re.compile(r"git\s+push\s+-f\b"), "Force push is blocked — this can destroy remote history."),
    (
        re.compile(r"git\s+push\s+.*--force-with-lease"),
        "Force push (with lease) is blocked — this can destroy remote history.",
    ),
    # Git tag (tagging is a release operation — must be done by a human or CI)
    (
        re.compile(r"git\s+tag\b"),
        "Git tagging is blocked — tags and releases should not be created locally.\n\n"
        "Recommended approach: create a GitHub Actions workflow with workflow_dispatch\n"
        "that handles tagging, version bumping, and changelog generation automatically.\n"
        "Place it at .github/workflows/release.yml with a dispatch trigger so you can\n"
        "run it from the GitHub UI or via `gh workflow run release.yml`.\n"
        "This keeps version control centralized, auditable, and out of local machines.",
    ),
]


def check_branch_guard(payload: dict) -> None:
    """Raise BlockAction if the Bash command targets main/master.

    Should only be called when tool_name == 'Bash'.
    Only checks the actual command portion — strips heredoc bodies and
    quoted commit messages to avoid false positives on message content.
    """
    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        return

    # Strip heredoc bodies (<<'EOF' ... EOF) and quoted strings to avoid
    # matching "main/master" inside commit messages or echo text
    check_text = re.sub(r"<<'?EOF'?.*", "", command, flags=re.DOTALL)
    # Also strip content inside -m "..." or -m '...'
    check_text = re.sub(r'-m\s+"[^"]*"', "-m MSG", check_text)
    check_text = re.sub(r"-m\s+'[^']*'", "-m MSG", check_text)

    for pattern, message in _BLOCKED_PATTERNS:
        if pattern.search(check_text):
            log(
                "branch_guard: blocked",
                {
                    "command": command[:200],
                    "pattern": pattern.pattern,
                    "session_id": payload.get("session_id", ""),
                },
            )
            raise BlockAction(f"BLOCKED: {message}")
