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
from pathlib import Path

from hooks._redis import get_redis, redis_key
from hooks.common import log
from hooks.config import AGENTIHOOKS_HOME
from hooks.hook_manager import BlockAction

# Per-turn branch-creation signal (CI Manifesto §14)
_BRANCH_SIGNAL_TYPE = "branch_create_signal"
# Per-turn PR-creation signal (CI Manifesto §15)
_PR_SIGNAL_TYPE = "pr_create_signal"
_BRANCH_SIGNAL_TTL = 300
_PR_SIGNAL_TTL = 14400  # session-scoped (CI Manifesto §15)
_PR_SIGNAL_MAX_COUNT = 3  # re-signal required after N PR creations


def _branch_signal_key(session_id: str) -> str:
    return redis_key(_BRANCH_SIGNAL_TYPE, session_id)


def _branch_signal_flag(session_id: str) -> Path:
    return AGENTIHOOKS_HOME / "prod_bypass" / f"{session_id}.branch"


def _pr_signal_key(session_id: str) -> str:
    return redis_key(_PR_SIGNAL_TYPE, session_id)


def _pr_signal_flag(session_id: str) -> Path:
    return AGENTIHOOKS_HOME / "prod_bypass" / f"{session_id}.pr"


def set_branch_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.setex(_branch_signal_key(session_id), _BRANCH_SIGNAL_TTL, "1")
        except Exception as e:
            log("branch_guard.set_branch_signal redis failed", {"error": str(e)})
    try:
        f = _branch_signal_flag(session_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("1")
    except Exception as e:
        log("branch_guard.set_branch_signal file failed", {"error": str(e)})


def clear_branch_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(_branch_signal_key(session_id))
        except Exception:
            pass
    try:
        _branch_signal_flag(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def _has_branch_signal(session_id: str) -> bool:
    try:
        from hooks.context.controls_toggle import is_controls_disabled

        if is_controls_disabled(session_id):
            return True
    except Exception:
        pass
    if not session_id:
        return False
    r = get_redis()
    if r:
        try:
            return bool(r.exists(_branch_signal_key(session_id)))
        except Exception:
            pass
    return _branch_signal_flag(session_id).exists()


def set_pr_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.setex(_pr_signal_key(session_id), _PR_SIGNAL_TTL, "1")
        except Exception as e:
            log("branch_guard.set_pr_signal redis failed", {"error": str(e)})
    try:
        f = _pr_signal_flag(session_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("1")
    except Exception as e:
        log("branch_guard.set_pr_signal file failed", {"error": str(e)})


def clear_pr_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(_pr_signal_key(session_id))
        except Exception:
            pass
    try:
        _pr_signal_flag(session_id).unlink(missing_ok=True)
    except Exception:
        pass
    clear_pr_counter(session_id)


def _pr_counter_key(session_id: str) -> str:
    return redis_key("pr_create_count", session_id)


def _pr_counter_flag(session_id: str) -> Path:
    return AGENTIHOOKS_HOME / "prod_bypass" / f"{session_id}.pr_count"


def increment_pr_counter(session_id: str) -> int:
    r = get_redis()
    if r:
        try:
            count = int(r.incr(_pr_counter_key(session_id)))
            r.expire(_pr_counter_key(session_id), _PR_SIGNAL_TTL)
            return count
        except Exception as e:
            log("branch_guard.increment_pr_counter redis failed", {"error": str(e)})
    try:
        f = _pr_counter_flag(session_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        existing = int(f.read_text().strip()) if f.exists() else 0
        count = existing + 1
        f.write_text(str(count))
        return count
    except Exception as e:
        log("branch_guard.increment_pr_counter file failed", {"error": str(e)})
    return 0


def _get_pr_counter(session_id: str) -> int:
    r = get_redis()
    if r:
        try:
            v = r.get(_pr_counter_key(session_id))
            return int(v) if v else 0
        except Exception:
            pass
    try:
        f = _pr_counter_flag(session_id)
        return int(f.read_text().strip()) if f.exists() else 0
    except Exception:
        return 0


def clear_pr_counter(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(_pr_counter_key(session_id))
        except Exception:
            pass
    try:
        _pr_counter_flag(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def _has_pr_signal(session_id: str) -> bool:
    try:
        from hooks.context.controls_toggle import is_controls_disabled

        if is_controls_disabled(session_id):
            return True
    except Exception:
        pass
    if not session_id:
        return False
    r = get_redis()
    if r:
        try:
            return bool(r.exists(_pr_signal_key(session_id)))
        except Exception:
            pass
    return _pr_signal_flag(session_id).exists()


# PR creation pattern (CI Manifesto §15). Blocks `gh pr create` in any form.
# Other gh pr subcommands (list/view/status/comment/review/edit/merge) pass.
_PR_CREATE_PATTERN = re.compile(r"\bgh\s+pr\s+create\b")

# PR base-branch enforcement — gh pr create must target main
_PR_BASE_MAIN_PATTERN = re.compile(r"\bgh\s+pr\s+create\b[^|&;\n]*--base\s+main\b")


# Branch-creation patterns (blocked without branch signal).
# Worktree commands are NOT blocked — worktrees are the operator-assigned
# parallel-work primitive (§14).
_BRANCH_CREATE_PATTERNS = [
    re.compile(r"\bgit\s+checkout\s+-[bB]\b"),
    re.compile(r"\bgit\s+switch\s+-[cC]\b"),
    # git branch <name> (creation form — not -d/-D/-a/-v/-l/-r/--list)
    re.compile(
        r"\bgit\s+branch\s+(?!(-[aAvVlLrRdD]|--list|--all|--verbose|--delete|--remotes|--sort|--contains|--merged|--no-merged|-m|-M|--move|--copy|-c|-C|--set-upstream-to|--unset-upstream|--edit-description|--format|--show-current))\S+"
    ),
]

_BLOCKED_PATTERNS = [
    # Push to main/master (direct push bypasses PR workflow)
    (
        re.compile(r"git\s+push\s+\S*\s+(origin\s+)?(HEAD:)?(?<![\w-])(main|master)(?![/\w-])"),
        "Pushing directly to main/master is blocked. Use gh pr create --base main instead.",
    ),
    # Merge into main/master (direct merge bypasses PR workflow)
    (
        re.compile(r"git\s+merge\s+[^|&;\n]*(?<![\w-])(main|master)(?![/\w-])"),
        "Direct merge into main/master is blocked. Create a PR instead.",
    ),
    # Rebase onto main/master
    (
        re.compile(r"git\s+rebase\s+[^|&;\n]*(?<![\w-])(main|master)(?![/\w-])"),
        "Rebasing onto main/master is blocked. Create a PR instead.",
    ),
    # Delete main/master branch
    (
        re.compile(r"git\s+branch\s+(-[dD]|--delete)\b.*(?<![\w-])(main|master)(?![/\w-])"),
        "Deleting main/master is blocked.",
    ),
    # Reset main/master (destructive — rewrites history)
    (
        re.compile(r"git\s+reset\s+[^|&;\n]*(?<![\w-])(main|master)(?![/\w-])"),
        "Resetting main/master is blocked — this rewrites history.",
    ),
    # Force push (any branch — can destroy remote history).
    # Bypass-eligible: short-circuited when 'disable controls' is active.
    (
        re.compile(r"git\s+push\s+--force(?!-with-lease)"),
        "Force push is blocked — this can destroy remote history.",
    ),
    (
        re.compile(r"git\s+push\s+-f\b"),
        "Force push is blocked — this can destroy remote history.",
    ),
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
    """Raise BlockAction if the Bash command targets main/master, or creates a
    branch without an active branch-creation signal (§14).

    Should only be called when tool_name == 'Bash'.
    Only checks the actual command portion — strips heredoc bodies and
    quoted commit messages to avoid false positives on message content.
    """
    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        return

    from hooks.context._strip import strip_non_command_content

    check_text = strip_non_command_content(command)

    try:
        from hooks.context.controls_toggle import is_controls_disabled

        _bypass_active = is_controls_disabled(payload.get("session_id", ""))
    except Exception:
        _bypass_active = False

    for pattern, message in _BLOCKED_PATTERNS:
        if pattern.search(check_text):
            if _bypass_active and (
                "Force push" in message
                or "Rebasing onto" in message
                or "Direct merge" in message
            ):
                # Force-push and rebase-onto-main are bypass-eligible per
                # controls-toggle rule. Direct-to-main push still caught by
                # the main/master push pattern above.
                continue
            log(
                "branch_guard: blocked",
                {
                    "command": command[:200],
                    "pattern": pattern.pattern,
                    "session_id": payload.get("session_id", ""),
                },
            )
            raise BlockAction(f"BLOCKED: {message}")

    # Branch creation — default-deny unless operator signaled this turn (§14)
    session_id = payload.get("session_id", "")
    for pattern in _BRANCH_CREATE_PATTERNS:
        if pattern.search(check_text):
            if _has_branch_signal(session_id):
                return
            log(
                "branch_guard: branch creation blocked",
                {"command": command[:200], "pattern": pattern.pattern, "session_id": session_id},
            )
            raise BlockAction(
                "BLOCKED: agent branch creation is disabled (CI Manifesto §14).\n"
                "Work on 'dev' (default) or an operator-assigned branch/worktree.\n"
                "To unlock for this turn, operator must include a branch signal "
                "(e.g. 'new branch', 'create branch', 'branch allowed')."
            )

    # PR creation — default-deny unless operator signaled this session (§15)
    if _PR_CREATE_PATTERN.search(check_text):
        if not _has_pr_signal(session_id):
            log(
                "branch_guard: PR creation blocked (no signal)",
                {"command": command[:200], "session_id": session_id},
            )
            raise BlockAction(
                "BLOCKED: agent PR creation is disabled (CI Manifesto §15).\n"
                "Agents commit and push — the operator decides when to open a PR.\n"
                "To unlock for this session, include a PR phrase in your message\n"
                "(e.g. 'open a PR', 'create a PR', 'make a PR', 'pr please')."
            )
        try:
            from hooks.context.controls_toggle import is_controls_disabled as _ctl_off

            _bypass_counter = _ctl_off(session_id)
        except Exception:
            _bypass_counter = False
        count = _get_pr_counter(session_id)
        if not _bypass_counter and count >= _PR_SIGNAL_MAX_COUNT:
            log(
                "branch_guard: PR creation blocked (counter limit)",
                {"count": count, "session_id": session_id},
            )
            raise BlockAction(
                f"BLOCKED: PR creation limit reached ({_PR_SIGNAL_MAX_COUNT} PRs this session).\n"
                "Include a PR phrase in your next message to re-authorize and reset the counter."
            )
        if not _PR_BASE_MAIN_PATTERN.search(check_text):
            log(
                "branch_guard: PR creation blocked (non-main base)",
                {"command": command[:200], "session_id": session_id},
            )
            raise BlockAction(
                "BLOCKED: PRs must target main only (--base main required).\n"
                "For dev branch changes, push directly — no PR needed.\n"
                "Correct form: gh pr create --base main ..."
            )
        new_count = increment_pr_counter(session_id)
        log("branch_guard: PR creation allowed", {"count": new_count, "session_id": session_id})


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
