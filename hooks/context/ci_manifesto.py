"""CI Manifesto injector — doctrine-as-context primitive.

Reads a single markdown file (the CI/release doctrine) and injects it into the
session context at SessionStart, with counter-gated re-injection on
UserPromptSubmit to fight attention decay.

Also exposes signal vocabulary parsed from the manifesto (§9) so enforcement
hooks (branch_guard, prod_lockdown) can derive their patterns from the same
source of truth rather than hardcoding.

Config (env vars via hooks.config):
    CI_MANIFESTO_ENABLED          (bool, default True)
    CI_MANIFESTO_PATH             (str,  default $HOME/dev/tcc-ecosystem/documents/anton/ANTON-CORE-CI-MANIFESTO.md)
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

_manifesto_cache: dict = {"path": "", "mtime": 0.0, "content": "", "release": [], "hotfix": [], "branch": [], "pr": []}
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
_DEFAULT_PR_SIGNALS = [
    "open a pr",
    "open the pr",
    "create a pr",
    "create the pr",
    "make a pr",
    "make the pr",
    "pr please",
    "pr allowed",
    "open pr",
    "create pr",
    "raise a pr",
    "submit a pr",
]


def _manifesto_path() -> Path:
    from hooks.config import CI_MANIFESTO_PATH

    return Path(CI_MANIFESTO_PATH).expanduser()


def _load() -> dict:
    """Read manifesto from disk (mtime-gated cache) and parse signal vocabulary."""
    path = _manifesto_path()
    try:
        if not path.is_file():
            return {
                "path": str(path),
                "mtime": 0.0,
                "content": "",
                "release": _DEFAULT_RELEASE_SIGNALS,
                "hotfix": _DEFAULT_HOTFIX_SIGNALS,
                "branch": _DEFAULT_BRANCH_SIGNALS,
                "pr": _DEFAULT_PR_SIGNALS,
            }
        mtime = path.stat().st_mtime
        if _manifesto_cache["path"] == str(path) and _manifesto_cache["mtime"] == mtime and _manifesto_cache["content"]:
            return _manifesto_cache
        content = path.read_text(encoding="utf-8")
        release, hotfix, branch, pr = _parse_signals(content)
        _manifesto_cache.update(
            {
                "path": str(path),
                "mtime": mtime,
                "content": content,
                "release": release,
                "hotfix": hotfix,
                "branch": branch,
                "pr": pr,
            }
        )
        return _manifesto_cache
    except Exception as e:
        log("ci_manifesto load failed", {"path": str(path), "error": str(e)})
        return {
            "path": str(path),
            "mtime": 0.0,
            "content": "",
            "release": _DEFAULT_RELEASE_SIGNALS,
            "hotfix": _DEFAULT_HOTFIX_SIGNALS,
            "branch": _DEFAULT_BRANCH_SIGNALS,
            "pr": _DEFAULT_PR_SIGNALS,
        }


_SECTION_RE = re.compile(
    r"\*\*(Release-gate signals|Hotfix signals)\*\*.*?```(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
# Branch Discipline and PR Discipline sections — both share the
# "Unlock (per-turn operator signal)" fenced-block convention. We match
# by section title (section numbering changes when new sections land).
_BRANCH_SECTION_RE = re.compile(
    r"##\s*\d+\.\s*Branch\s+Discipline.*?###\s*Unlock\s*\([^)]*operator signal\).*?```(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_PR_SECTION_RE = re.compile(
    r"##\s*\d+\.\s*PR\s+Discipline.*?###\s*Unlock\s*\([^)]*operator signal\).*?```(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def _parse_signals(content: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """Extract signal phrases from §9 (release/hotfix), §14 (branch), §15 (PR) of the manifesto."""
    release: list[str] = []
    hotfix: list[str] = []
    branch: list[str] = []
    pr: list[str] = []
    for m in _SECTION_RE.finditer(content):
        header = m.group(1).lower()
        block = m.group(2).strip()
        phrases = [
            line.strip().lower() for line in block.splitlines() if line.strip() and not line.strip().startswith("#")
        ]
        if "release" in header:
            release = phrases
        elif "hotfix" in header:
            hotfix = phrases
    bm = _BRANCH_SECTION_RE.search(content)
    if bm:
        branch = [
            line.strip().lower()
            for line in bm.group(1).strip().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    pm = _PR_SECTION_RE.search(content)
    if pm:
        pr = [
            line.strip().lower()
            for line in pm.group(1).strip().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    if not release:
        release = _DEFAULT_RELEASE_SIGNALS
    if not hotfix:
        hotfix = _DEFAULT_HOTFIX_SIGNALS
    if not branch:
        branch = _DEFAULT_BRANCH_SIGNALS
    if not pr:
        pr = _DEFAULT_PR_SIGNALS
    return release, hotfix, branch, pr


def get_release_signals() -> list[str]:
    return _load()["release"]


def get_hotfix_signals() -> list[str]:
    return _load()["hotfix"]


def get_branch_signals() -> list[str]:
    return _load().get("branch", _DEFAULT_BRANCH_SIGNALS)


def get_pr_signals() -> list[str]:
    return _load().get("pr", _DEFAULT_PR_SIGNALS)


_NEGATION_PREFIXES = (
    "don't ",
    "dont ",
    "do not ",
    "not ",
    "never ",
    "shouldn't ",
    "shouldnt ",
    "should not ",
    "won't ",
    "wont ",
    "will not ",
    "can't ",
    "cannot ",
)


def _signal_match(text: str, signals: list[str]) -> bool:
    """Check if any signal phrase is present WITHOUT being negated."""
    if not text:
        return False
    low = text.lower()
    for phrase in signals:
        idx = low.find(phrase)
        if idx == -1:
            continue
        # Check for negation in the 20 chars before the match
        prefix = low[max(0, idx - 20) : idx].strip()
        if any(prefix.endswith(neg.strip()) for neg in _NEGATION_PREFIXES):
            continue
        return True
    return False


def contains_release_signal(text: str) -> bool:
    return _signal_match(text, get_release_signals())


def contains_hotfix_signal(text: str) -> bool:
    return _signal_match(text, get_hotfix_signals())


def contains_branch_signal(text: str) -> bool:
    return _signal_match(text, get_branch_signals())


def contains_pr_signal(text: str) -> bool:
    return _signal_match(text, get_pr_signals())


# Claude Code injects hook stdout and additionalContext into the model's
# context with a documented 10,000-char hard cap (over → harness silently
# writes to a temp file and the model gets a filepath, not the body).
#
# We do NOT enforce a default cap on the manifesto inject — operators who
# want full doctrine in context should keep the env var unset and accept
# the upstream tradeoff. Set CI_MANIFESTO_MAX_BYTES=N to opt in to
# truncation when you have multiple things competing for SessionStart
# budget.
#
# CI_MANIFESTO_MAX_BYTES=0 (default) → unbounded; emit full manifesto.
# CI_MANIFESTO_MAX_BYTES=7500          → cap to ~7.5 KB with trailer.
_INJECTION_TRAILER_TEMPLATE = (
    "\n[TRUNCATED — the full doctrine is at: {path}]\n"
    "Read that path with the Read tool when you need section details.\n"
    "=== END CI MANIFESTO ===\n"
)
_INJECTION_HEADER_TEMPLATE = (
    "=== CI MANIFESTO (auto-injected doctrine — source of truth) ===\n"
    "Source: {path}\n\n"
)


def _injection_budget_bytes() -> int:
    """Read budget from env each call so operators can dial via .env edits
    without a session restart. 0 = no cap."""
    from hooks.config import CI_MANIFESTO_MAX_BYTES

    return CI_MANIFESTO_MAX_BYTES


def _build_injection() -> str:
    data = _load()
    content = data["content"]
    if not content:
        return ""
    path_str = str(data["path"])
    header = _INJECTION_HEADER_TEMPLATE.format(path=path_str)
    full_trailer = "\n=== END CI MANIFESTO ===\n"
    full_payload = header + content + full_trailer

    budget = _injection_budget_bytes()
    if budget <= 0 or len(full_payload.encode("utf-8")) <= budget:
        return full_payload
    # Need to truncate. Compute room left for content after header + trailer.
    trailer = _INJECTION_TRAILER_TEMPLATE.format(path=path_str)
    overhead = len((header + trailer).encode("utf-8"))
    body_budget = max(0, budget - overhead)
    body_bytes = content.encode("utf-8")[:body_budget]
    body = body_bytes.decode("utf-8", errors="ignore")
    return header + body + trailer


def inject_on_session_start() -> None:
    """Emit manifesto as additionalContext at SessionStart.

    Default OFF — the manifesto is now appended to ~/.claude/CLAUDE.md by
    agentihooks init (memory channel, no 2KB hook cap, zero per-session
    cost). Set CI_MANIFESTO_RUNTIME_INJECT=true to restore legacy runtime
    injection.
    """
    try:
        from hooks.config import CI_MANIFESTO_ENABLED, CI_MANIFESTO_RUNTIME_INJECT

        if not CI_MANIFESTO_ENABLED or not CI_MANIFESTO_RUNTIME_INJECT:
            return
        payload = _build_injection()
        if not payload:
            log("ci_manifesto: empty payload (file missing?)", {"path": str(_manifesto_path())})
            return
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": payload}}))
    except Exception as e:
        log("ci_manifesto inject_on_session_start failed", {"error": str(e)})


def maybe_refresh(session_id: str) -> None:
    """Counter-gated re-injection on UserPromptSubmit.

    Like inject_on_session_start, this is gated by CI_MANIFESTO_RUNTIME_INJECT.
    With manifesto in CLAUDE.md, periodic re-injection is unnecessary —
    CLAUDE.md content stays in context for the whole session.
    """
    try:
        from hooks.config import (
            CI_MANIFESTO_ENABLED,
            CI_MANIFESTO_REFRESH_EVERY,
            CI_MANIFESTO_RUNTIME_INJECT,
        )

        if not CI_MANIFESTO_ENABLED or not CI_MANIFESTO_RUNTIME_INJECT:
            return
        if CI_MANIFESTO_REFRESH_EVERY <= 0:
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
