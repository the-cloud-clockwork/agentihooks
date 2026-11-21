"""PreToolUse enforcement for sustained swarm blockers: a category stuck for an hour or more opens the master coding
window automatically and injects a once-a-minute notice naming each sustained category, putting the master in
troubleshooting mode with every tool it needs. A swarm's panic opens the same window for its master and denies the
pushes, reruns, dispatches and deploys of every other session bound to that swarm; sessions bound to another swarm
and unbound sessions are never held. A push or dispatch whose every ref is a registered fix branch, or the head branch
of a registered fix pull request, is spared, and so is a rerun of one run or job whose head branch, read from GitHub,
is the checkout's branch or upstream and one. Swarm publish, which pushes from its own process, asks the same panic
rule through held_push before it pushes.
Fails open on a store error."""

import os
import re
import subprocess
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hooks.context import master_coding
from hooks.context.shell_commands import (
    _FROZEN_COMMANDS as _FROZEN_COMMANDS,
)
from hooks.context.shell_commands import (
    _GLOBAL_VALUES as _GLOBAL_VALUES,
)
from hooks.context.shell_commands import (
    _command as _command,
)
from hooks.context.shell_commands import (
    _dispatches as _dispatches,
)
from hooks.context.shell_commands import (
    _ref as _ref,
)
from hooks.context.shell_commands import (
    _targets as _targets,
)

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
_RERUN_JOB_FLAGS = frozenset({"--job", "-j"})
_RERUN_FLAGS = frozenset({"--failed", "-d", "--debug"})
_RERUN_JOB = re.compile(r"(?:--job=|-j=?)(.+)")
_RERUN_ID = re.compile(r"(?:runs|jobs)/[0-9]+")
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
    return any(words[: len(prefix)] == list(prefix) for prefix in _FROZEN_COMMANDS) or _dispatches(words)


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
    """Return whether a frozen command writes only registered fix branches of the checkout where it runs: a push or
    dispatch whose every ref is one, or a rerun of one run or job whose head is the checkout's branch or its upstream
    on origin and one; git -C moves that directory, and any other git global option, or a git config that may route
    the push elsewhere, keeps it frozen."""
    words, options = _command(tokens)
    targets = _targets(words)
    if targets is None or any(option != "-C" for option, _ in options):
        return False
    here = _directory(cwd, [value for _, value in options])
    if words[:3] == ["gh", "run", "rerun"]:
        return _works_on_fix(here, fixes, words)
    return _on_fix_branch(here, fixes, targets) and not (words[:2] == ["git", "push"] and _routed(here))


def _routed(cwd: str) -> bool:
    """Return whether git config may send a push from the checkout to another remote or ref: a push refspec, push url
    or mirror on origin, a push url rewrite, tag following, a push or tracking remote other than origin, a push default
    other than simple or current, or more than one push url on origin, since git pushes to every one; a config that
    cannot be read counts as routed."""
    lines = _git(cwd, "config", "--list").splitlines()
    if not lines or any(_ROUTED.fullmatch(line) for line in lines):
        return True
    return len(_git(cwd, "remote", "get-url", "--push", "--all", "origin").splitlines()) > 1


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
    """Return whether every ref the command writes is a registered fix branch of the repository origin names in the
    checkout at cwd, a fix branch link naming both or a fix pull request whose head branch it is: each ref the command
    names, or the checked out branch when it names none."""
    if not (cwd and fixes):
        return False
    named = [target.removeprefix("refs/heads/") for target in targets] or [_git(cwd, "branch", "--show-current")]
    return all(_registered(cwd, fixes, branch) for branch in named)


def _works_on_fix(cwd: str, fixes: tuple[str, ...], words: list[str]) -> bool:
    """Return whether the one run or job a rerun's words name has its head on the checked out branch at cwd, or on the
    branch it tracks on origin, and that branch is a registered fix of the repository origin names there; the head is
    read only for such a branch, from that repository, and a run that cannot be read stays frozen."""
    from scripts.swarm import panic
    from scripts.swarm.stack import repo_key
    from scripts.swarm.store import SwarmError

    resource = _rerun(words)
    if not (cwd and fixes and resource):
        return False
    branch = _git(cwd, "branch", "--show-current")
    kept = {name for name in (branch, _tracked(cwd, branch)) if _registered(cwd, fixes, name)}
    if not kept:
        return False
    repository = repo_key(_git(cwd, "remote", "get-url", "origin")).removeprefix("github.com/")
    try:
        return panic.run_branch(repository, resource) in kept
    except SwarmError:
        return False


def _rerun(words: list[str]) -> str:
    """Return the Actions resource of the one run or job a gh run rerun's words name: runs/ID for a run id, jobs/ID for
    a --job or -j id; an empty string when they name none, more than one, an id that is not a number, or an option
    other than --failed, -d and --debug."""
    named: list[str] = []
    for before, word in pairwise(words[2:]):
        job = _RERUN_JOB.fullmatch(word)
        if before in _RERUN_JOB_FLAGS:
            named[-1] += word
        elif word in _RERUN_JOB_FLAGS:
            named.append("jobs/")
        elif job is not None:
            named.append(f"jobs/{job[1]}")
        elif word not in _RERUN_FLAGS:
            named.append(f"runs/{word}")
    return named[0] if len(named) == 1 and _RERUN_ID.fullmatch(named[0]) else ""


def _tracked(cwd: str, branch: str) -> str:
    """Return the branch on origin that branch tracks in the checkout at cwd, or an empty name when it tracks no branch
    there."""
    if _git(cwd, "config", f"branch.{branch}.remote") != "origin":
        return ""
    return _git(cwd, "config", f"branch.{branch}.merge").removeprefix("refs/heads/")


def _registered(cwd: str, fixes: tuple[str, ...], branch: str) -> bool:
    """Return whether branch is a registered fix of the repository origin names in the checkout at cwd: a fix branch
    link naming both, or a fix pull request whose head branch it is."""
    from scripts.swarm import panic
    from scripts.swarm.stack import repo_key

    origin = branch and _git(cwd, "remote", "get-url", "origin")
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
