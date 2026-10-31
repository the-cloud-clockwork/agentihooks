"""PreToolUse enforcement for sustained swarm blockers: a category stuck for an hour or more opens the master coding
window automatically, injects a once-a-minute notice naming each sustained category, and denies any tool call that
is not part of the fix. Fleet panic mode opens the same window and denies every other session's pushes, reruns,
dispatches and deploys. Fails open on a store error."""

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from hooks.context import master_coding

if TYPE_CHECKING:
    from scripts.swarm.blockers import Blocker
    from scripts.swarm.store import RedisStore

NOTICE = (
    "BLOCKER QUEUE on swarm {slug}: {categories} sustained for an hour or more. Fix this now: the master coding "
    "window is open; other work stays denied until each category is cleared with "
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
BLOCK = (
    "BLOCKED: sustained blockers on swarm {slug} ({categories}) hold the master to the fix: reads, read-only shell, "
    "edits inside a worktree, git, gh, agentihooks swarm {slug} merge|blockers, msg and ledger commands."
)
_READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead"})
_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
_READ_COMMANDS = frozenset({"cat", "head", "tail", "ls", "wc", "grep", "find", "pwd"})
_WORKTREES = Path.home() / "dev" / "worktrees"


def _slug(session_id: str) -> str:
    return str(master_coding.seat(session_id).get("slug", ""))


def _store() -> "RedisStore | None":
    from scripts.swarm import store
    from scripts.swarm.store import SwarmError, Unreachable

    try:
        return cast(Any, store).connect()
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


def _in_worktree(value: str, cwd: str) -> bool:
    path = Path(value).expanduser()
    target = (path if path.is_absolute() else Path(cwd or ".") / path).resolve()
    return target == _WORKTREES or target.is_relative_to(_WORKTREES)


def _allowed_tokens(tokens: list[str], slug: str) -> bool:
    if not tokens:
        return False
    name = Path(tokens[0]).name
    if name in _READ_COMMANDS or name in ("git", "gh"):
        return True
    if name != "agentihooks" or len(tokens) < 2:
        return False
    if tokens[1] == "swarm":
        return len(tokens) >= 4 and tokens[2] == slug and tokens[3] in ("merge", "blockers", "panic")
    return tokens[1] in ("msg", "ledger")


def _allowed_bash(command: str, slug: str) -> bool:
    from hooks.context.shell_commands import commands

    found = commands(command)
    return bool(found) and all(_allowed_tokens(tokens, slug) for tokens in found)


def _allowed(tool_name: str, tool_input: dict[str, Any], cwd: str, slug: str) -> bool:
    if tool_name in _READ_TOOLS or tool_name.startswith("mcp__serena__"):
        return True
    if tool_name == "Bash":
        return _allowed_bash(str(tool_input.get("command") or ""), slug)
    if tool_name in _WRITE_TOOLS:
        value = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return bool(value) and _in_worktree(value, cwd)
    return False


def _now_ms(store: "RedisStore") -> int:
    seconds, microseconds = cast(tuple[int, int], cast(Any, store).redis.time())
    return seconds * 1000 + microseconds // 1000


def pretool(
    tool_name: str, tool_input: dict[str, Any], session_id: str, cwd: str = ""
) -> tuple[str | None, str | None]:
    """Returns (notice, block): the notice to inject and the reason to deny this call, either may be None."""
    if not session_id:
        return None, None
    if not master_coding.is_master(session_id):
        return None, _frozen(tool_name, tool_input)
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
    if _allowed(tool_name, tool_input, cwd, slug):
        return notice, None
    return notice, BLOCK.format(slug=slug, categories=categories)


def _panic_found(store: "RedisStore", now_ms: int) -> "dict[str, Blocker]":
    from redis.exceptions import RedisError

    from scripts.swarm import blockers, panic

    try:
        current = panic.state(store)
    except RedisError:
        return {}
    if current is None:
        return {}
    return {"panic": blockers.Blocker("panic", current.since, now_ms, 0, current.reason)}


def _freezes(tokens: list[str]) -> bool:
    words = [Path(tokens[0]).name, *tokens[1:]] if tokens else []
    return any(words[: len(prefix)] == list(prefix) for prefix in _FROZEN_COMMANDS)


def _frozen(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    if tool_name != "Bash":
        return None
    from hooks.context.shell_commands import commands

    if not any(_freezes(tokens) for tokens in commands(str(tool_input.get("command") or ""))):
        return None
    store = _store()
    if store is None:
        return None
    found = _panic_found(store, 0)
    return FROZEN.format(reason=found["panic"].detail) if found else None


def checked(payload: dict[str, Any]) -> str | None:
    notice, block = pretool(
        str(payload.get("tool_name") or ""),
        cast(dict[str, Any], payload.get("tool_input") or {}),
        str(payload.get("session_id", "")),
        str(payload.get("cwd", "")),
    )
    if notice:
        from hooks.common import inject_context

        inject_context(notice, also_log=False, skip_compression=True, kind="alert")
    return block
