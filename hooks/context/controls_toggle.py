"""Controls toggle — session-level bypass for CI-manifesto signal gates.

Operator says "disable controls" → branch creation, PR creation, release-gate
merges, and hotfix-category prod ops are short-circuited until the operator
says "enable controls" or the parent session ends.

Storage is a single shared flag file (not per-session) so that subagents
spawned during the bypass window inherit the parent's state automatically.
The flag records the owning session_id so SessionEnd clears it only if the
parent session is the one ending.

The HARD floor (direct push to main, commit-on-main, secrets-in-files,
GitHub server-side ruleset) is unaffected by this toggle.

Public API:
    set_controls_disabled(session_id)
    clear_controls_disabled(session_id)   — only clears if owner matches
    is_controls_disabled(session_id=None) — global check; subagents inherit
    contains_disable_signal(text) / contains_enable_signal(text)
"""

from __future__ import annotations

import re

from hooks._redis import get_redis, redis_key
from hooks.common import log

CONTROLS_TYPE = "controls_disabled"
from hooks.config import AGENTIHOOKS_HOME

_FLAG_DIR = AGENTIHOOKS_HOME / "controls_flags"
_GLOBAL_FLAG = _FLAG_DIR / "active.flag"
_GLOBAL_REDIS_KEY = redis_key(CONTROLS_TYPE, "_global")

_RE_DISABLE = re.compile(
    r"\b(disable|turn\s+off|deactivate|kill)\s+controls\b",
    re.IGNORECASE,
)
_RE_ENABLE = re.compile(
    r"\b(enable|turn\s+on|activate|restore)\s+controls\b",
    re.IGNORECASE,
)


def contains_disable_signal(text: str) -> bool:
    return bool(text) and bool(_RE_DISABLE.search(text))


def contains_enable_signal(text: str) -> bool:
    return bool(text) and bool(_RE_ENABLE.search(text))


def set_controls_disabled(session_id: str) -> None:
    """Activate bypass mode globally; record session_id as owner."""
    owner = session_id or "unknown"
    r = get_redis()
    if r:
        try:
            r.set(_GLOBAL_REDIS_KEY, owner)
        except Exception as e:
            log("controls_toggle.set redis failed", {"error": str(e)})
    try:
        _FLAG_DIR.mkdir(parents=True, exist_ok=True)
        _GLOBAL_FLAG.write_text(owner)
    except Exception as e:
        log("controls_toggle.set file failed", {"error": str(e)})


def _read_owner() -> str | None:
    r = get_redis()
    if r:
        try:
            v = r.get(_GLOBAL_REDIS_KEY)
            if v:
                return v.decode() if isinstance(v, bytes) else str(v)
        except Exception:
            pass
    try:
        if _GLOBAL_FLAG.exists():
            return _GLOBAL_FLAG.read_text().strip()
    except Exception:
        pass
    return None


def clear_controls_disabled(session_id: str | None = None, force: bool = False) -> None:
    """Clear bypass globally.

    If session_id is given and is NOT the recorded owner, the clear is a no-op
    (subagent ending should not deactivate the parent's bypass). Pass force=True
    to bypass the owner check (used by explicit 'enable controls' from any session).
    """
    owner = _read_owner()
    if not force and session_id and owner and owner != session_id:
        return
    r = get_redis()
    if r:
        try:
            r.delete(_GLOBAL_REDIS_KEY)
        except Exception:
            pass
    try:
        _GLOBAL_FLAG.unlink(missing_ok=True)
    except Exception:
        pass


def is_controls_disabled(session_id: str | None = None) -> bool:
    """Return True if bypass mode is active. Global — subagents inherit."""
    return _read_owner() is not None
