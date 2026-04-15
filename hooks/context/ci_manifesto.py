"""CI Manifesto injector — doctrine-as-context primitive.

Reads a single markdown file (the CI/release doctrine) and injects it into the
session context at SessionStart, with counter-gated re-injection on
UserPromptSubmit to fight attention decay.

Also exposes signal vocabulary parsed from the manifesto (§9) so enforcement
hooks (branch_guard, prod_lockdown) can derive their patterns from the same
source of truth rather than hardcoding.

Config (env vars via hooks.config):
    CI_MANIFESTO_ENABLED          (bool, default True)
    CI_MANIFESTO_PATH             (str,  default $HOME/dev/tccw-ecosystem/documents/anton/ANTON-CORE-CI-MANIFESTO.md)
    CI_MANIFESTO_REFRESH_EVERY    (int,  default 8 turns)

Public API:
    inject_on_session_start()             — one-shot injection
    maybe_refresh(session_id)             — counter-gated re-inject
    get_release_signals() -> list[str]    — parsed phrases from §9
    get_hotfix_signals() -> list[str]     — parsed phrases from §9
    contains_release_signal(text) -> bool
    contains_hotfix_signal(text) -> bool
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hooks.common import log

_manifesto_cache: dict = {"path": "", "mtime": 0.0, "content": "", "release": [], "hotfix": [], "branch": []}
_session_counter: dict[str, int] = {}


# Fallback signals (used if manifesto missing / unparseable). Must match §9.
_DEFAULT_RELEASE_SIGNALS = [
    "release to prod",
    "push to prod",
    "let's release",
    "ship it",
    "release gate",
    "merge to main",
    "merge the pr",
    "merge it",
]
_DEFAULT_HOTFIX_SIGNALS = [
    "hotfix",
    "prod is broken",
    "prod is down",
    "outage",
    "--emergency-prod",
    "prod override",
    "emergency",
]
_DEFAULT_BRANCH_SIGNALS = [
    "new branch",
    "create branch",
    "make a branch",
    "branch allowed",
    "use branch",
    "branch it",
    "feature branch",
]


def _manifesto_path() -> Path:
    from hooks.config import CI_MANIFESTO_PATH

    return Path(CI_MANIFESTO_PATH).expanduser()


def _load() -> dict:
    """Read manifesto from disk (mtime-gated cache) and parse signal vocabulary."""
    path = _manifesto_path()
    try:
        if not path.is_file():
            return {"path": str(path), "mtime": 0.0, "content": "", "release": _DEFAULT_RELEASE_SIGNALS, "hotfix": _DEFAULT_HOTFIX_SIGNALS, "branch": _DEFAULT_BRANCH_SIGNALS}
        mtime = path.stat().st_mtime
        if _manifesto_cache["path"] == str(path) and _manifesto_cache["mtime"] == mtime and _manifesto_cache["content"]:
            return _manifesto_cache
        content = path.read_text(encoding="utf-8")
        release, hotfix, branch = _parse_signals(content)
        _manifesto_cache.update({"path": str(path), "mtime": mtime, "content": content, "release": release, "hotfix": hotfix, "branch": branch})
        return _manifesto_cache
    except Exception as e:
        log("ci_manifesto load failed", {"path": str(path), "error": str(e)})
        return {"path": str(path), "mtime": 0.0, "content": "", "release": _DEFAULT_RELEASE_SIGNALS, "hotfix": _DEFAULT_HOTFIX_SIGNALS, "branch": _DEFAULT_BRANCH_SIGNALS}


_SECTION_RE = re.compile(
    r"\*\*(Release-gate signals|Hotfix signals)\*\*.*?```(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
# Branch signals live under §14 as a plain fenced block.
_BRANCH_SECTION_RE = re.compile(
    r"###\s*Unlock\s*\(per-turn operator signal\).*?```(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def _parse_signals(content: str) -> tuple[list[str], list[str], list[str]]:
    """Extract signal phrases from §9 (release/hotfix) and §14 (branch) of the manifesto."""
    release: list[str] = []
    hotfix: list[str] = []
    branch: list[str] = []
    for m in _SECTION_RE.finditer(content):
        header = m.group(1).lower()
        block = m.group(2).strip()
        phrases = [line.strip().lower() for line in block.splitlines() if line.strip() and not line.strip().startswith("#")]
        if "release" in header:
            release = phrases
        elif "hotfix" in header:
            hotfix = phrases
    bm = _BRANCH_SECTION_RE.search(content)
    if bm:
        branch = [line.strip().lower() for line in bm.group(1).strip().splitlines() if line.strip() and not line.strip().startswith("#")]
    if not release:
        release = _DEFAULT_RELEASE_SIGNALS
    if not hotfix:
        hotfix = _DEFAULT_HOTFIX_SIGNALS
    if not branch:
        branch = _DEFAULT_BRANCH_SIGNALS
    return release, hotfix, branch


def get_release_signals() -> list[str]:
    return _load()["release"]


def get_hotfix_signals() -> list[str]:
    return _load()["hotfix"]


def get_branch_signals() -> list[str]:
    return _load().get("branch", _DEFAULT_BRANCH_SIGNALS)


def contains_release_signal(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(p in low for p in get_release_signals())


def contains_hotfix_signal(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(p in low for p in get_hotfix_signals())


def contains_branch_signal(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(p in low for p in get_branch_signals())


def _build_injection() -> str:
    data = _load()
    content = data["content"]
    if not content:
        return ""
    return (
        "=== CI MANIFESTO (auto-injected doctrine — source of truth) ===\n"
        f"Source: {data['path']}\n\n"
        f"{content}\n"
        "=== END CI MANIFESTO ===\n"
    )


def inject_on_session_start() -> None:
    """Emit manifesto as additionalContext at SessionStart."""
    try:
        from hooks.config import CI_MANIFESTO_ENABLED

        if not CI_MANIFESTO_ENABLED:
            return
        payload = _build_injection()
        if not payload:
            log("ci_manifesto: empty payload (file missing?)", {"path": str(_manifesto_path())})
            return
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": payload}}))
    except Exception as e:
        log("ci_manifesto inject_on_session_start failed", {"error": str(e)})


def maybe_refresh(session_id: str) -> None:
    """Counter-gated re-injection on UserPromptSubmit."""
    try:
        from hooks.config import CI_MANIFESTO_ENABLED, CI_MANIFESTO_REFRESH_EVERY

        if not CI_MANIFESTO_ENABLED or CI_MANIFESTO_REFRESH_EVERY <= 0:
            return
        _session_counter[session_id] = _session_counter.get(session_id, 0) + 1
        if _session_counter[session_id] % CI_MANIFESTO_REFRESH_EVERY != 0:
            return
        payload = _build_injection()
        if not payload:
            return
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": payload}}))
    except Exception as e:
        log("ci_manifesto maybe_refresh failed", {"error": str(e)})


def clear_session_state(session_id: str) -> None:
    _session_counter.pop(session_id, None)
