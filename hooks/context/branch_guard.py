"""Branch Guard — blocks destructive git operations on main/master.

Prevents pushes, merges, rebases, branch deletions targeting main/master,
force pushes, git tagging, and commits while HEAD is on main/master.
Read-only operations (pull, diff, log, status) are allowed.

Public API:
    check_branch_guard(payload)      — called from on_pre_tool_use for Bash
    check_commit_on_main(payload)    — called from on_pre_tool_use for Bash
"""

import os
import re
import subprocess

from hooks.common import log
from hooks.hook_manager import BlockAction

_BLOCKED_PATTERNS = [
    # Push to main/master (direct push bypasses PR workflow)
    (
        re.compile(r"git\s+push\s+\S*\s+(origin\s+)?(HEAD:)?(main|master)\b"),
        "Pushing directly to main/master is blocked. Use gh pr create --base main instead.",
    ),
    # Merge into main/master (direct merge bypasses PR workflow)
    (
        re.compile(r"git\s+merge\s+.*\b(main|master)\b"),
        "Direct merge into main/master is blocked. Create a PR instead.",
    ),
    # Rebase onto main/master
    (
        re.compile(r"git\s+rebase\s+.*\b(main|master)\b"),
        "Rebasing onto main/master is blocked. Create a PR instead.",
    ),
    # Delete main/master branch
    (
        re.compile(r"git\s+branch\s+(-[dD]|--delete)\b.*\b(main|master)\b"),
        "Deleting main/master is blocked.",
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


_COMMIT_PATTERN = re.compile(r"\bgit\s+commit\b")
_CD_PREFIX_PATTERN = re.compile(r"^\s*cd\s+(\S+)\s*(?:&&|;)")


def _resolve_cwd(command: str, payload_cwd: str) -> str:
    """Return the effective cwd — honors `cd <path> && ...` prefix in command."""
    m = _CD_PREFIX_PATTERN.match(command)
    if m:
        path = m.group(1).strip('"').strip("'")
        if os.path.isdir(path):
            return path
    if payload_cwd and os.path.isdir(payload_cwd):
        return payload_cwd
    return os.getcwd()


def _current_branch(cwd: str) -> str:
    """Return the current git branch name, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def check_commit_on_main(payload: dict) -> None:
    """Block `git commit` when HEAD is on main/master.

    AI should always work on a feature branch. Committing on main (even
    locally) pollutes main history and encourages the wrong workflow.
    Should only be called when tool_name == 'Bash'.
    """
    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command", "")
    if not command or not _COMMIT_PATTERN.search(command):
        return

    payload_cwd = payload.get("cwd") or payload.get("tool_input", {}).get("cwd") or ""
    cwd = _resolve_cwd(command, payload_cwd)
    branch = _current_branch(cwd)
    if branch in ("main", "master"):
        log(
            "branch_guard: commit on main blocked",
            {
                "branch": branch,
                "command": command[:200],
                "cwd": cwd,
                "session_id": payload.get("session_id", ""),
            },
        )
        raise BlockAction(
            f"BLOCKED: git commit on '{branch}' branch is not allowed.\n"
            f"Create a feature branch first:\n"
            f"  git checkout -b feat/<short-description>\n"
            f"Then commit and open a PR to main when ready."
        )
