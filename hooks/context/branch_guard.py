"""Branch Guard — blocks git operations targeting main/master branches.

Universal, non-negotiable guardrail. Any Bash command that pushes to,
merges into, rebases onto, checks out, resets, or commits directly on
main or master is blocked with exit code 2.

Only `dev` (or feature branches) can receive commits and pushes.

Public API:
    check_branch_guard(payload)  — called from on_pre_tool_use for Bash
"""

import re

from hooks.common import log
from hooks.hook_manager import BlockAction

# Patterns that target main/master as a destination
_BLOCKED_PATTERNS = [
    # Push to main/master
    re.compile(r"git\s+push\s+.*\b(main|master)\b"),
    re.compile(r"git\s+push\s+.*HEAD:(main|master)"),
    # Checkout main/master (switching to it to commit)
    re.compile(r"git\s+checkout\s+(main|master)\b"),
    re.compile(r"git\s+switch\s+(main|master)\b"),
    # Merge into main/master
    re.compile(r"git\s+merge\s+.*\b(main|master)\b"),
    # Rebase onto main/master
    re.compile(r"git\s+rebase\s+(main|master)\b"),
    # Reset main/master
    re.compile(r"git\s+reset\s+.*\b(main|master)\b"),
    # Force push (any branch — extra dangerous)
    re.compile(r"git\s+push\s+--force"),
    re.compile(r"git\s+push\s+-f\b"),
    re.compile(r"git\s+push\s+.*--force-with-lease"),
    # Branch delete main/master
    re.compile(r"git\s+branch\s+-[dD]\s+(main|master)\b"),
    # gh pr merge (merges PRs — could target main)
    re.compile(r"gh\s+pr\s+merge"),
]

_BLOCK_MESSAGE = (
    "BLOCKED: Git operations targeting main/master are not allowed. "
    "Only dev or feature branches can receive commits and pushes.\n\n"
    "If you need to push, use: git push origin HEAD (pushes current branch).\n"
    "If you need to merge to main, create a PR instead."
)


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

    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(check_text):
            log("branch_guard: blocked", {
                "command": command[:200],
                "pattern": pattern.pattern,
                "session_id": payload.get("session_id", ""),
            })
            raise BlockAction(_BLOCK_MESSAGE)
