"""PreToolUse enforcement for sustained swarm blockers: a category stuck for an hour or more opens the master coding
window automatically, injects a once-a-minute notice naming each sustained category, and denies any tool call that
is not part of the fix. Never fires for a non-master session; fails open on a store error."""

from pathlib import Path

from hooks.context import master_coding

NOTICE = (
    "BLOCKER QUEUE on swarm {slug}: {categories} sustained for an hour or more. Fix this now: the master coding "
    "window is open; other work stays denied until each category is cleared with "
    "agentihooks swarm {slug} blockers clear <category>."
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
    return master_coding.seat(session_id).get("slug", "")


def _store():
    from scripts.swarm.store import SwarmError, Unreachable, connect

    try:
        return connect()
    except (SwarmError, Unreachable):
        return None


def _sustained(store, slug: str, now_ms: int) -> dict:
    from scripts.swarm import blockers

    try:
        return blockers.sustained(store, slug, now_ms)
    except Exception:
        return {}


def _describe(found: dict) -> str:
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
        return len(tokens) >= 4 and tokens[2] == slug and tokens[3] in ("merge", "blockers")
    return tokens[1] in ("msg", "ledger")


def _allowed_bash(command: str, slug: str) -> bool:
    from hooks.context.shell_commands import commands

    found = commands(command)
    return bool(found) and all(_allowed_tokens(tokens, slug) for tokens in found)


def _allowed(tool_name: str, tool_input: dict, cwd: str, slug: str) -> bool:
    if tool_name in _READ_TOOLS or tool_name.startswith("mcp__serena__"):
        return True
    if tool_name == "Bash":
        return _allowed_bash(str(tool_input.get("command") or ""), slug)
    if tool_name in _WRITE_TOOLS:
        value = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return bool(value) and _in_worktree(value, cwd)
    return False


def pretool(tool_name: str, tool_input: dict, session_id: str, cwd: str = "") -> tuple[str | None, str | None]:
    """Returns (notice, block): the notice to inject and the reason to deny this call, either may be None."""
    if not (session_id and master_coding.is_master(session_id)):
        return None, None
    slug = _slug(session_id)
    if not slug:
        return None, None
    store = _store()
    if store is None:
        return None, None
    from scripts.swarm import blockers

    now_ms = store.redis.time()
    now_ms = now_ms[0] * 1000 + now_ms[1] // 1000
    found = _sustained(store, slug, now_ms)
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


def checked(payload: dict) -> str | None:
    try:
        notice, block = pretool(
            str(payload.get("tool_name") or ""),
            payload.get("tool_input") or {},
            payload.get("session_id", ""),
            payload.get("cwd", ""),
        )
    except Exception as e:
        from hooks.common import log

        log("blocker window check failed", {"error": str(e)})
        return None
    if notice:
        from hooks.common import inject_context

        inject_context(notice, also_log=False, skip_compression=True, kind="alert")
    return block
