"""Production operations lockdown — PreToolUse hard guardrail.

Default-denies prod-impacting commands; unlocks per-turn when the operator's
message contains a release-gate or hotfix signal (CI Manifesto §9).

Blocked by default:
  - Docker/image operations with :latest/:prod/:stable tags  (hotfix bypass only)
  - gh workflow run release.yml                              (release-gate OR hotfix)
  - gh pr merge targeting main/master                        (release-gate OR hotfix)

Signal vocabulary is parsed from the CI Manifesto (source of truth).
Fallback vocabulary in ci_manifesto.py mirrors manifesto §9.

Note: kubectl anton-prod is NOT blocked (operator unlocked 2026-04-15).
"""

import re
from pathlib import Path

from hooks._redis import get_redis, redis_key
from hooks.common import log
from hooks.hook_manager import BlockAction

BYPASS_TYPE = "prod_lockdown_bypass"
BYPASS_TTL = 300  # 5 min — per-turn legacy bypass (--emergency-prod)
_SESSION_SIGNAL_TTL = 14400  # 4 hours — session-scoped signals (release, hotfix)

# Legacy hotfix/emergency vocabulary — kept for backwards-compat.
# Full signal vocabulary now sourced from CI Manifesto via ci_manifesto.py.
BYPASS_PHRASES: list[str] = ["--emergency-prod", "prod override", "emergency"]

# Block category: "hotfix" → only hotfix signals unlock
#                 "release" → release-gate OR hotfix signals unlock
_BLOCKED: list[tuple[re.Pattern, str, str, str]] = [
    (
        re.compile(r"(ghcr\.io|docker\.io)/[^/\s]+/[^:\s]+:(latest|prod|stable)\b", re.I),
        "image tag :latest/:prod/:stable",
        "use :dev or a branch-specific tag",
        "hotfix",
    ),
    (
        re.compile(r"\bdocker\b[^|&;\n]*(push|tag|build)[^|&;\n]*(latest|prod|stable)\b", re.I),
        "docker op with production tag",
        "use :dev or a branch-specific tag",
        "hotfix",
    ),
    (
        re.compile(r"\bgh\b[^|&;\n]*workflow\s+run\s+release\.yml\b", re.I),
        "release.yml workflow trigger",
        "release-gate signal required — see CI Manifesto §4",
        "release",
    ),
    (
        re.compile(
            r"\bgh\b[^|&;\n]*\bpr\s+merge\b[^|&;\n]*(--base\s+(main|master)\b|\b(main|master)\b(?!\s*\.))",
            re.I,
        ),
        "gh pr merge to main/master",
        "release-gate signal required — see CI Manifesto §4",
        "release",
    ),
]


def _bypass_key(session_id: str) -> str:
    return redis_key(BYPASS_TYPE, session_id)


def _flag_file(session_id: str) -> Path:
    return Path.home() / ".agentihooks" / "prod_bypass" / f"{session_id}.flag"


def set_bypass(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.setex(_bypass_key(session_id), BYPASS_TTL, "1")
        except Exception as e:
            log("prod_lockdown.set_bypass redis failed", {"error": str(e)})
    try:
        f = _flag_file(session_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("1")
    except Exception as e:
        log("prod_lockdown.set_bypass file failed", {"error": str(e)})


def clear_bypass(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(_bypass_key(session_id))
        except Exception:
            pass
    try:
        _flag_file(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def is_bypass_active(session_id: str) -> bool:
    r = get_redis()
    if r:
        try:
            return bool(r.exists(_bypass_key(session_id)))
        except Exception:
            pass
    return _flag_file(session_id).exists()


def contains_bypass_phrase(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in BYPASS_PHRASES)


def _release_key(session_id: str) -> str:
    return redis_key("release_gate_signal", session_id)


def _release_flag(session_id: str) -> Path:
    return Path.home() / ".agentihooks" / "prod_bypass" / f"{session_id}.release"


def set_release_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.setex(_release_key(session_id), _SESSION_SIGNAL_TTL, "1")
        except Exception as e:
            log("prod_lockdown.set_release_signal redis failed", {"error": str(e)})
    try:
        f = _release_flag(session_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("1")
    except Exception as e:
        log("prod_lockdown.set_release_signal file failed", {"error": str(e)})


def clear_release_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(_release_key(session_id))
        except Exception:
            pass
    try:
        _release_flag(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def _hotfix_key(session_id: str) -> str:
    return redis_key("hotfix_signal", session_id)


def _hotfix_flag(session_id: str) -> Path:
    return Path.home() / ".agentihooks" / "prod_bypass" / f"{session_id}.hotfix"


def set_hotfix_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.setex(_hotfix_key(session_id), _SESSION_SIGNAL_TTL, "1")
        except Exception as e:
            log("prod_lockdown.set_hotfix_signal redis failed", {"error": str(e)})
    try:
        f = _hotfix_flag(session_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("1")
    except Exception as e:
        log("prod_lockdown.set_hotfix_signal file failed", {"error": str(e)})


def clear_hotfix_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(_hotfix_key(session_id))
        except Exception:
            pass
    try:
        _hotfix_flag(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def _has_hotfix_signal(session_id: str) -> bool:
    if not session_id:
        return False
    r = get_redis()
    if r:
        try:
            return bool(r.exists(_hotfix_key(session_id)))
        except Exception:
            pass
    return _hotfix_flag(session_id).exists()


def _strip_safe_content(command: str) -> str:
    """Strip heredoc bodies and commit messages before pattern matching.

    Same pre-processing as branch_guard to avoid false positives on
    commit message content, heredoc bodies, and echo text.
    """
    # Strip heredoc bodies (<<'EOF' ... EOF) — these are data, not commands
    check = re.sub(r"<<'?EOF'?.*", "", command, flags=re.DOTALL)
    # Strip git commit messages (-m "..." or -m '...')
    check = re.sub(r'-m\s+"[^"]*"', "-m MSG", check)
    check = re.sub(r"-m\s+'[^']*'", "-m MSG", check)
    return check


def _has_release_signal(session_id: str) -> bool:
    """Check redis/flag-file for release-gate signal set this turn by UserPromptSubmit."""
    r = get_redis()
    if r:
        try:
            return bool(r.exists(redis_key("release_gate_signal", session_id)))
        except Exception:
            pass
    try:
        return (Path.home() / ".agentihooks" / "prod_bypass" / f"{session_id}.release").exists()
    except Exception:
        return False


def check_prod_lockdown(payload: dict) -> None:
    session_id = payload.get("session_id", "")
    # Legacy full bypass (--emergency-prod etc.) — unlocks everything, per-turn
    full_bypass = bool(session_id and is_bypass_active(session_id))
    # Session-scoped hotfix signal — unlocks everything
    hotfix_unlock = bool(session_id and _has_hotfix_signal(session_id))
    # Session-scoped release-gate signal — unlocks only release-category blocks
    release_unlock = bool(session_id and _has_release_signal(session_id))

    command = payload.get("tool_input", {}).get("command", "")
    if not command:
        return

    check_text = _strip_safe_content(command)

    for pattern, name, reason, category in _BLOCKED:
        if not pattern.search(check_text):
            continue
        if full_bypass or hotfix_unlock:
            return
        if category == "release" and release_unlock:
            return
        hint = (
            "Add a release-gate phrase (e.g. 'merge to main', 'ship it', 'release to prod') or --emergency-prod."
            if category == "release"
            else "Add --emergency-prod / 'prod override' / 'emergency' to your message."
        )
        raise BlockAction(f"BLOCKED [{name}]: {reason}\n{hint}")
