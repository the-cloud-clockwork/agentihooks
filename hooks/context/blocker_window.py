"""PreToolUse enforcement for sustained swarm blockers: a category stuck for an hour or more opens the master coding
window automatically and injects a once-a-minute notice naming each sustained category, putting the master in
troubleshooting mode with every tool it needs. A swarm's panic opens the same window for its master and denies the
pushes, reruns, dispatches and deploys of every other session bound to that swarm; sessions bound to another swarm
and unbound sessions are never held. A session on a registered fix branch, or on the head branch of a registered fix
pull request, keeps reruns in its repository and the pushes and dispatches that write only that branch. Swarm
publish, which pushes from its own process, asks the same panic rule through held_push before it pushes.
Fails open on a store error."""

import os
import re
import subprocess
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
    "agentihooks swarm {slug} panic on --fix <pr url> pauses the swarm's agent starts, cancels every CI run of its "
    "agents but the fix and the merge queue, and pauses the lab's pull request grading but the fix, printing what it "
    "cancelled and paused."
)
FROZEN = (
    "BLOCKED: panic mode holds swarm {slug} ({reason}). Its sessions' pushes, reruns, dispatches and deploys wait "
    "until the master's fix brings it back to business as usual; agentihooks swarm {slug} panic status shows the level."
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
_GIT_VALUES = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--attr-source"}
)
_PUSH_WIDENS = frozenset({"--all", "--branches", "--mirror", "--tags", "--follow-tags", "--prune", "--repo"})
_REDIRECTION = re.compile(r"([^<>]*)([<>].*)")
_GLOBAL_VALUES = {"git": _GIT_VALUES, "gh": frozenset({"-R", "--repo"})}
_GH_REPO = re.compile(r"-R.*|--repo(=.*)?")
_GIT_ENVIRONMENT = re.compile(r"\bGIT_(DIR|WORK_TREE|COMMON_DIR|NAMESPACE|CONFIG\w*)=")
_ROUTED = re.compile(
    r"remote\.origin\.(push|pushurl|mirror)=.*"
    r"|(remote\.pushdefault|branch\..+\.(remote|pushremote))=(?!origin$).*"
    r"|push\.default=(?!simple$|current$).*"
    r"|url\..+\.pushinsteadof=.*"
    r"|push\.followtags(?!=(false|no|off|0)$).*"
)


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
    found = {**_sustained(store, slug, now_ms), **_panic_found(store, slug, now_ms)}
    if not found:
        if master_coding.opened_for_blockers(session_id):
            master_coding.close(session_id)
        return None, None
    if not master_coding.is_open(session_id):
        master_coding.open_for_blockers(session_id, now_ms / 1000)
    categories = _describe(found)
    notice = NOTICE.format(slug=slug, categories=categories) if blockers.due_notice(store, slug, now_ms) else None
    return notice, None


def _bound() -> str | None:
    """Return the swarm this session is bound to; None or an empty name for a session outside every swarm."""
    return os.environ.get("AGENTIHOOKS_SWARM")


def _panic_state(store: "RedisStore", slug: str) -> "Panic | None":
    from redis.exceptions import RedisError

    from scripts.swarm import panic

    try:
        return panic.state(store, slug)
    except RedisError:
        return None


def _panic_found(store: "RedisStore", slug: str, now_ms: int) -> "dict[str, Blocker]":
    from scripts.swarm import blockers

    current = _panic_state(store, slug)
    if current is None:
        return {}
    return {"panic": blockers.Blocker("panic", current.since, now_ms, 0, current.reason)}


def _freezes(tokens: list[str]) -> bool:
    words = _command(tokens)[0]
    return any(words[: len(prefix)] == list(prefix) for prefix in _FROZEN_COMMANDS)


def _frozen(tool_name: str, tool_input: dict[str, Any], cwd: str) -> str | None:
    slug = _bound()
    if tool_name != "Bash" or not slug:
        return None
    from hooks.context.shell_commands import located

    command = str(tool_input.get("command"))
    found = [(tokens, folders) for tokens, folders in located(command) if _freezes(tokens)]
    if not found:
        return None
    store = _store()
    current = None if store is None else _panic_state(store, slug)
    if current is None or (
        _GIT_ENVIRONMENT.search(command) is None
        and all(_spared(tokens, _directory(cwd, folders), current.fixes) for tokens, folders in found)
    ):
        return None
    return FROZEN.format(slug=slug, reason=current.reason)


def _directory(cwd: str, folders: list[str]) -> str:
    """Return the directory a command runs in: the session directory changed by each cd before it, or an empty
    string without a session directory."""
    if not cwd:
        return ""
    here = Path(cwd)
    for folder in folders:
        here = here / Path(folder).expanduser()
    return str(here)


def _spared(tokens: list[str], cwd: str, fixes: tuple[str, ...]) -> bool:
    """Return whether a frozen command writes only the registered fix branch checked out where it runs: git -C moves
    that directory, and any other git global option, or a git config that may route the push elsewhere, keeps it
    frozen."""
    words, options = _command(tokens)
    targets = _targets(words)
    if targets is None or any(option != "-C" for option, _ in options):
        return False
    here = _directory(cwd, [value for _, value in options])
    return _on_fix_branch(here, fixes, targets) and not (words[:2] == ["git", "push"] and _routed(here))


def _words(tokens: list[str]) -> list[str]:
    """Return a simple command's tokens without its redirections: a redirection that ends in its operator takes the
    next token as its target, and text before a < or > stays a word unless it names a file descriptor."""
    kept = [token for token in tokens if token != "&"]
    targets = {index + 1 for index, token in enumerate(kept) if token.endswith(("<", ">"))}
    words: list[str] = []
    for index, token in enumerate(kept):
        found = _REDIRECTION.fullmatch(token)
        if index in targets:
            continue
        if found is None:
            words.append(token)
        elif found.group(1) and not found.group(1).isdigit():
            words.append(found.group(1))
    return words


def _command(tokens: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Return a simple command's words without redirections, its program's base name first, and the git or gh global
    options before its subcommand, taken out of those words as (option, value) pairs."""
    words = _words(tokens)
    if not words:
        return [], []
    program, rest = Path(words[0]).name, iter(words[1:])
    values = _GLOBAL_VALUES.get(program)
    options: list[tuple[str, str]] = []
    for word in rest:
        if values is None or not word.startswith("-"):
            return [program, word, *rest], options
        options.append((word, next(rest, "") if word in values else ""))
    return [program], options


def _targets(words: list[str]) -> tuple[str, ...] | None:
    """Return the refs a command a fix branch session may run writes: none for a rerun or for a push of the checked
    out branch, a dispatch's ref and a push's refspec destinations; None for any other command, for a gh command
    aimed at another repository and for a push that may write more than its refspecs."""
    if words[:1] == ["gh"] and any(_GH_REPO.fullmatch(word) for word in words):
        return None
    if words[:3] == ["gh", "run", "rerun"]:
        return ()
    if words[:3] == ["gh", "workflow", "run"]:
        ref = _ref(words[3:])
        return (ref,) if ref else None
    if words[:2] == ["git", "push"]:
        return _pushed(words[2:])
    return None


def _ref(words: list[str]) -> str:
    """Return the last ref a workflow dispatch names with --ref or -r, or an empty name when it names none and runs
    on the default branch."""
    found = ""
    for index, word in enumerate(words):
        if word in ("--ref", "-r"):
            found = words[index + 1] if index + 1 < len(words) else ""
        elif word.startswith("--ref="):
            found = word.removeprefix("--ref=")
        elif word.startswith("-r"):
            found = word.removeprefix("-r").removeprefix("=")
    return found


def _pushed(words: list[str]) -> tuple[str, ...] | None:
    """Return the refs a push to origin writes, or None when it names another remote or an option that pushes more
    than its refspecs; a push with no refspec writes the checked out branch and names none."""
    if any(word.partition("=")[0] in _PUSH_WIDENS for word in words):
        return None
    named = [word for word in words if not word.startswith("-")]
    if named[:1] not in ([], ["origin"]):
        return None
    return _destinations(named[1:])


def _destinations(refspecs: list[str]) -> tuple[str, ...]:
    """Return the ref each refspec writes; a bare HEAD writes the checked out branch and names none."""
    parts = [refspec.removeprefix("+").partition(":") for refspec in refspecs]
    return tuple(target if colon else source for source, colon, target in parts if colon or source != "HEAD")


def _routed(cwd: str) -> bool:
    """Return whether git config may send a push from the checkout to another remote or ref: a push refspec, push url
    or mirror on origin, a push url rewrite, tag following, a push or tracking remote other than origin, or a push
    default other than simple or current; a config that cannot be read counts as routed."""
    lines = _git(cwd, "config", "--list").splitlines()
    return not lines or any(_ROUTED.fullmatch(line) for line in lines)


def _git(cwd: str, *args: str) -> str:
    """Return one git read's output in the session's directory, or an empty string when it fails."""
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


def _on_fix_branch(cwd: str, fixes: tuple[str, ...], targets: tuple[str, ...]) -> bool:
    """Return whether the session works in the repository and on the branch of a registered fix, a fix branch link
    naming both or a fix pull request whose head branch it is, and every ref the command names is that branch."""
    if not (cwd and fixes):
        return False
    branch = _git(cwd, "branch", "--show-current")
    if not branch or any(target not in (branch, f"refs/heads/{branch}") for target in targets):
        return False
    return _registered(cwd, fixes, branch)


def _registered(cwd: str, fixes: tuple[str, ...], branch: str) -> bool:
    """Return whether branch is a registered fix of the repository origin names in the checkout at cwd: a fix branch
    link naming both, or a fix pull request whose head branch it is."""
    from scripts.swarm import panic
    from scripts.swarm.stack import repo_key

    origin = _git(cwd, "remote", "get-url", "origin")
    if not origin:
        return False
    repository = repo_key(origin)
    if (repository, branch) in {panic.branch_fix(fix) for fix in fixes}:
        return True
    changes = f"{repository}/pull/"
    return any(repo_key(url).startswith(changes) and _fix_branch(url) == branch for url in fixes)


def held_push(cwd: str, branch: str, slug: str) -> str | None:
    """Return the panic refusal for a push of swarm ``slug`` to origin that writes branch from the checkout at cwd, or
    None when no panic holds that swarm, the store cannot be read, or branch is a registered fix of that checkout's
    repository and git config cannot route the push elsewhere."""
    store = _store()
    current = None if store is None else _panic_state(store, slug)
    if current is None or (_registered(cwd, current.fixes, branch) and not _routed(cwd)):
        return None
    return FROZEN.format(slug=slug, reason=current.reason)


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
