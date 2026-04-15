"""Production operations lockdown — PreToolUse hard guardrail.

Blocks commands that could affect production systems unless the operator's
current-turn message contains a bypass phrase.

Blocked operations:
  - Docker/image operations with :latest/:prod/:stable tags
  - gh workflow run release.yml

Note: kubectl anton-prod is NOT blocked (operator unlocked 2026-04-15).
AI has full prod namespace access.

Bypass phrases (any one of): --emergency-prod | prod override | emergency
Bypass is per-turn: set in UserPromptSubmit, cleared in Stop.
"""

import re
from pathlib import Path

from hooks._redis import get_redis, redis_key
from hooks.common import log
from hooks.hook_manager import BlockAction

BYPASS_TYPE = "prod_lockdown_bypass"
BYPASS_TTL = 300  # 5 min — covers the longest expected tool-call sequence per turn

BYPASS_PHRASES: list[str] = ["--emergency-prod", "prod override", "emergency"]

_BLOCKED: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"(ghcr\.io|docker\.io)/[^/\s]+/[^:\s]+:(latest|prod|stable)\b", re.I),
        "image tag :latest/:prod/:stable",
        "use :dev or a branch-specific tag",
    ),
    (
        re.compile(r"\bdocker\b[^|&;\n]*(push|tag|build)[^|&;\n]*(latest|prod|stable)\b", re.I),
        "docker op with production tag",
        "use :dev or a branch-specific tag",
    ),
    (
        re.compile(r"\bgh\b[^|&;\n]*workflow\s+run\s+release\.yml\b", re.I),
        "release.yml workflow trigger",
        'operator must include --emergency-prod to trigger a release',
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


def check_prod_lockdown(payload: dict) -> None:
    session_id = payload.get("session_id", "")
    if session_id and is_bypass_active(session_id):
        return

    command = payload.get("tool_input", {}).get("command", "")
    if not command:
        return

    check_text = _strip_safe_content(command)

    for pattern, name, reason in _BLOCKED:
        if pattern.search(check_text):
            raise BlockAction(
                f"BLOCKED [{name}]: {reason}\n"
                f"Add --emergency-prod to your message to bypass for this turn."
            )
