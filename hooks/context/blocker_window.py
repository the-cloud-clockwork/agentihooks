"""PreToolUse enforcement for sustained swarm blockers: a category stuck for an hour or more opens the master coding
window automatically and injects a once-a-minute notice naming each sustained category, putting the master in
troubleshooting mode with every tool it needs. Fleet panic mode opens the same window and denies every other
session's pushes, reruns, dispatches and deploys; a session on the head branch of a registered fix keeps its pushes,
reruns and dispatches.
Fails open on a store error."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from hooks.context import master_coding

if TYPE_CHECKING:
    from scripts.swarm.blockers import Blocker
    from scripts.swarm.panic import Panic
    from scripts.swarm.store import RedisStore

NOTICE = (
    "BLOCKER QUEUE on swarm {slug}: {categories} sustained for an hour or more. Fix this now: the master coding "
    "window is open; troubleshoot and fix before any other work, and clear each category with "
    "agentihooks swarm {slug} blockers clear <category>. Stop everything unnecessary for the fix: "
    "agentihooks swarm {slug} panic on --fix <pr url> pauses agent starts and cancels every CI run but the fix "
    "and the merge queue, printing what it cancelled."
)
FROZEN = (
    "BLOCKED: panic mode holds the fleet ({reason}). Pushes, reruns, dispatches and deploys wait until the master's "
    "fix brings the fleet back to business as usual; agentihooks swarm <slug> panic status shows the level."
)
_FROZEN_COMMANDS = (
    ("git", "push"),
    ("gh", "run", "rerun"),
    ("gh", "workflow", "run"),
    ("kubectl", "apply"),
    ("kubectl", "rollout"),
    ("helm", "upgrade"),
    ("helm", "install"),
    ("argocd", "app", "sync"),
)
_FIX_COMMANDS = _FROZEN_COMMANDS[:3]


def _slug(session_id: str) -> str:
    return str(master_coding.seat(session_id).get("slug", ""))


def _store() -> "RedisStore | None":
    from scripts.swarm import store
    from scripts.swarm.store import SwarmError, Unreachable

    try:
        return getattr(store, "connect")()
    except (SwarmError, Unreachable):
        return None


def _sustained(store: "RedisStore", slug: str, now_ms: int) -> "dict[str, Blocker]":
    from redis.exceptions import RedisError

    from scripts.swarm import blockers

    try:
        return blockers.sustained(store, slug, now_ms)
    except RedisError:
        return {}


def _describe(found: "dict[str, Blocker]") -> str:
    return ", ".join(
        f"{category} for {(blocker.last_seen - blocker.first_seen) // 60_000} minutes"
        for category, blocker in sorted(found.items())
    )


def _now_ms(store: "RedisStore") -> int:
    seconds, microseconds = getattr(store, "redis").time()
    return seconds * 1000 + microseconds // 1000


def pretool(
    tool_name: str, tool_input: dict[str, Any], session_id: str, cwd: str = ""
) -> tuple[str | None, str | None]:
    """Returns (notice, block): the notice to inject and the reason to deny this call, either may be None."""
    if not session_id:
        return None, None
    if not master_coding.is_master(session_id):
        return None, _frozen(tool_name, tool_input, cwd)
    slug = _slug(session_id)
    if not slug:
        return None, None
    store = _store()
    if store is None:
        return None, None
    from scripts.swarm import blockers

    now_ms = _now_ms(store)
    found = {**_sustained(store, slug, now_ms), **_panic_found(store, now_ms)}
    if not found:
        if master_coding.opened_for_blockers(session_id):
            master_coding.close(session_id)
        return None, None
    if not master_coding.is_open(session_id):
        master_coding.open_for_blockers(session_id, now_ms / 1000)
    categories = _describe(found)
    notice = NOTICE.format(slug=slug, categories=categories) if blockers.due_notice(store, slug, now_ms) else None
    return notice, None


def _panic_state(store: "RedisStore") -> "Panic | None":
    from redis.exceptions import RedisError

    from scripts.swarm import panic

    try:
        return panic.state(store)
    except RedisError:
        return None


def _panic_found(store: "RedisStore", now_ms: int) -> "dict[str, Blocker]":
    from scripts.swarm import blockers

    current = _panic_state(store)
    if current is None:
        return {}
    return {"panic": blockers.Blocker("panic", current.since, now_ms, 0, current.reason)}


def _freezes(tokens: list[str]) -> bool:
    words = [Path(tokens[0]).name, *tokens[1:]] if tokens else []
    return any(words[: len(prefix)] == list(prefix) for prefix in _FROZEN_COMMANDS)


def _frozen(tool_name: str, tool_input: dict[str, Any], cwd: str = "") -> str | None:
    if tool_name != "Bash":
        return None
    from hooks.context.shell_commands import commands

    found = [tokens for tokens in commands(str(tool_input.get("command"))) if _freezes(tokens)]
    if not found:
        return None
    store = _store()
    current = None if store is None else _panic_state(store)
    if current is None or (all(_spares(tokens) for tokens in found) and _on_fix_branch(cwd, current.fixes)):
        return None
    return FROZEN.format(reason=current.reason)


def _spares(tokens: list[str]) -> bool:
    words = [Path(tokens[0]).name, *tokens[1:]]
    return any(words[: len(prefix)] == list(prefix) for prefix in _FIX_COMMANDS)


def _git(cwd: str, *args: str) -> str:
    """Return one git read's output in the session's directory, or an empty string when it fails."""
    import subprocess

    try:
        found = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return found.stdout.strip() if found.returncode == 0 else ""


def _fix_branch(url: str) -> str:
    """Return the head branch of one registered fix pull request, or an empty string when it cannot be read."""
    from scripts.swarm import panic
    from scripts.swarm.store import SwarmError

    try:
        return panic.fix_branch(url)
    except SwarmError:
        return ""


def _on_fix_branch(cwd: str, fixes: tuple[str, ...]) -> bool:
    """Return whether the session works in the repository and on the head branch of a registered fix pull request."""
    from scripts.swarm.stack import repo_key

    branch = _git(cwd, "branch", "--show-current") if cwd and fixes else ""
    origin = _git(cwd, "remote", "get-url", "origin") if branch else ""
    if not origin:
        return False
    changes = f"{repo_key(origin)}/pull/"
    return any(repo_key(url).startswith(changes) and _fix_branch(url) == branch for url in fixes)


def checked(payload: Any) -> str | None:
    tool_input: dict[str, Any] = payload.get("tool_input") or {}
    notice, block = pretool(
        str(payload.get("tool_name") or ""),
        tool_input,
        str(payload.get("session_id", "")),
        str(payload.get("cwd", "")),
    )
    if notice:
        from hooks.common import inject_context

        inject_context(notice, also_log=False, skip_compression=True, kind="alert")
    return block
