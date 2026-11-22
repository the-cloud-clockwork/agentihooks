"""Swarm panic mode: each swarm's error counter over the last hour, the alarm that engages that swarm's panic at
THRESHOLD errors, the CI cancellation that frees runners from the swarm's own runs for the named fixes and every
swarm's open fixer tasks, and the business-as-usual level that releases it. A panic holds only its swarm; the legacy
fleet record reads as panic for the swarm its reason names.

THRESHOLD is three times the busiest measured hour, 19 to 24 tick step failures per hour across the fleet, measured
when one swarm ran; each swarm's own count is held to it.

A step or a pass that merely runs long, up to the golden pass duration (`scripts.tick.engine.GOLDEN_PASS_S`),
never reaches either counter here: `count_error` only takes a category a caller chose to report, and the one
caller wired to a tick step (`scripts.swarm.timing.step`) reports only on a raised exception, never on a step
recorded late; `count_pass` only takes the ``ok`` a caller computed, and that caller
(`scripts.swarm.health.spawn_stall.watch`) sets it from whether any step raised, never from the pass's wall time.
"""

import json
import math
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from threading import Lock, Thread
from typing import Any, cast

from scripts.swarm import lab_control, naming
from scripts.swarm.store import PREFIX, RedisStore, SwarmError
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import SourceControl
from scripts.tick.ports.source_control import ChangeRef, ChangeState, RepositoryRun

FLEET = "fleet"
HOUR_MS = 60 * 60 * 1000
WINDOW_MS = 15 * 60 * 1000
THRESHOLD = 60
RELEASE_LEVEL = 0.9
RELEASE_ERRORS = THRESHOLD // 4
CANCEL_EVERY_MS = 60 * 1000
METRIC = "agentihooks.swarm.errors"
MERGE_QUEUE = "merge_group"
QUEUE_BRANCH = "gh-readonly-queue/"
PUSH = "push"
RELEASE_EVENT = "release"
RELEASE_TAG = re.compile(r"v\d+\.\d+\.\d+")
WORKFLOW_RUN = "workflow_run"
TRIGGER_TITLE = re.compile(r".+ for (\S+)")
REPOS = (
    "the-cloud-clockwork/agentihooks",
    "the-cloud-clockwork/agentihooks-bundle",
    "the-cloud-clockwork/antoncore",
)
PROTECTED = frozenset({"dev", "main"})
FIX_LINK = re.compile(
    r"https://github\.com/([^/]+/[^/]+)/(?:pull/([0-9]+)|tree/(?!refs/)([A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*))"
)
ACTIVE = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
TOOL_SECONDS = 60.0
RUN_READ_SECONDS = 10.0
MERGED_FIXES = "merged-fixes"
LEGACY_REASON = re.compile(r"pressed on swarm (\S+)")
FIXER_TASK = re.compile(r"promo-[0-9]+-[0-9]+|crew-[0-9]+(?:-[0-9]+)?-fix-[0-9]+")
FIXERS = "panic-fixers"
FIXERS_MS = 15 * 60 * 1000
DONE = "done"
LAB = "lab"
Ports = Callable[[str], SourceControl]
SWEEP = Lock()


@dataclass(frozen=True)
class Panic:
    since: int
    reason: str
    auto: bool
    fixes: tuple[str, ...]


def _key(part: str) -> str:
    return f"{PREFIX}:{FLEET}:{part}"


def _swarm_key(slug: str, part: str) -> str:
    return f"{PREFIX}:{slug}:panic:{part}"


def _client(store: RedisStore) -> Any:
    return getattr(store, "redis")


def _emit(slug: str, category: str) -> None:
    from hooks.observability import otel

    otel.add_counter(METRIC, 1, {"swarm": slug, "category": category})


def count_error(store: RedisStore, slug: str, category: str, now_ms: int) -> None:
    """Counts one error in the swarm's window and on the OpenTelemetry counter."""
    key = _swarm_key(slug, "errors")
    _client(store).zadd(key, {f"{category}|{now_ms}|{uuid.uuid4().hex}": now_ms})
    _client(store).zremrangebyscore(key, 0, now_ms - HOUR_MS - 1)
    _emit(slug, category)


def errors_since(store: RedisStore, slug: str, since_ms: int) -> int:
    return int(_client(store).zcount(_swarm_key(slug, "errors"), since_ms, math.inf))


def errors_by_category(store: RedisStore, slug: str, since_ms: int) -> dict[str, int]:
    found: dict[str, int] = {}
    for member in _client(store).zrangebyscore(_swarm_key(slug, "errors"), since_ms, math.inf):
        category = str(member).split("|")[0]
        found[category] = found.get(category, 0) + 1
    return found


def count_pass(store: RedisStore, slug: str, ok: bool, now_ms: int) -> None:
    """Records one of the swarm's tick passes and whether every step in it finished without a failure."""
    key = _swarm_key(slug, "passes-ok" if ok else "passes-failed")
    _client(store).zadd(key, {f"{now_ms}|{uuid.uuid4().hex}": now_ms})
    _client(store).zremrangebyscore(key, 0, now_ms - WINDOW_MS - 1)


def level(store: RedisStore, slug: str, now_ms: int) -> float:
    """Business-as-usual level: the share of the swarm's tick passes in the last WINDOW_MS with no failed step."""
    since = now_ms - WINDOW_MS
    ok = int(_client(store).zcount(_swarm_key(slug, "passes-ok"), since, math.inf))
    failed = int(_client(store).zcount(_swarm_key(slug, "passes-failed"), since, math.inf))
    return ok / (ok + failed) if ok + failed else 1.0


def stable(store: RedisStore, slug: str, now_ms: int) -> bool:
    return (
        level(store, slug, now_ms) >= RELEASE_LEVEL and errors_since(store, slug, now_ms - WINDOW_MS) < RELEASE_ERRORS
    )


def _legacy(raw: str | None, slug: str) -> str | None:
    """Return the legacy fleet panic record when its reason names swarm ``slug``, else None."""
    found = None if raw is None else LEGACY_REASON.fullmatch(json.loads(raw)["reason"])
    return raw if found is not None and found[1] == slug else None


def state(store: RedisStore, slug: str) -> Panic | None:
    """Return the panic holding swarm ``slug``: its own record, else the legacy fleet record whose reason names it."""
    client = _client(store)
    raw = client.get(_swarm_key(slug, "state")) or _legacy(client.get(_key("panic")), slug)
    if raw is None:
        return None
    row = json.loads(raw)
    return Panic(row["since"], row["reason"], row["auto"], tuple(row["fixes"]))


def engage(store: RedisStore, slug: str, reason: str, auto: bool, fixes: Iterable[str], now_ms: int) -> Panic:
    """Turns the swarm's panic on, or adds fixes to the panic already on; a person pressing on with no fix makes it
    manual."""
    current = state(store, slug)
    added = tuple(fixes)
    found = Panic(
        current.since if current else now_ms,
        current.reason if current else reason,
        (current.auto and (auto or bool(added))) if current else auto,
        tuple(sorted({*(current.fixes if current else ()), *added})),
    )
    _client(store).set(_swarm_key(slug, "state"), json.dumps(found.__dict__))
    return found


def drop(store: RedisStore, slug: str, fix: str) -> Panic:
    """Removes one fix from the swarm's panic, keeping its start, reason, mode and other fixes; refuses a fix the panic
    does not hold and a swarm no panic holds."""
    current = state(store, slug)
    if current is None:
        raise SwarmError(f"no panic holds swarm {slug}")
    if fix not in current.fixes:
        raise SwarmError(f"the panic on swarm {slug} holds no fix {fix}")
    found = Panic(current.since, current.reason, current.auto, tuple(kept for kept in current.fixes if kept != fix))
    _client(store).set(_swarm_key(slug, "state"), json.dumps(found.__dict__))
    return found


def release(store: RedisStore, slug: str, now_ms: int) -> bool:
    """Turns the swarm's panic off, with the legacy fleet record that names it, only when the swarm is stable and back
    at RELEASE_LEVEL of business as usual."""
    if not stable(store, slug, now_ms):
        return False
    client = _client(store)
    legacy = [_key("panic")] if _legacy(client.get(_key("panic")), slug) else []
    client.delete(_swarm_key(slug, "state"), _swarm_key(slug, MERGED_FIXES), *legacy)
    return True


def alarm(store: RedisStore, slug: str, now_ms: int) -> str:
    """Engages the swarm's panic at THRESHOLD of its errors in the last hour; releases an automatic panic once
    stable."""
    current = state(store, slug)
    if current is None:
        count = errors_since(store, slug, now_ms - HOUR_MS)
        if count < THRESHOLD:
            return ""
        engage(store, slug, f"{count} errors in the last hour", True, (), now_ms)
        return f"panic engaged: {count} errors in the last hour"
    if current.auto and release(store, slug, now_ms):
        resumed = lab_release(store, slug)
        return f"panic released: the fleet is back at business as usual{f'; {resumed}' if resumed else ''}"
    return ""


def owner(store: RedisStore, slug: str) -> Callable[[str], bool]:
    """Return the check whether a branch belongs to swarm ``slug``: a branch one of its agents cut, the plain agent name
    on the swarm's code, after any folder such as diffcheck/ and before any suffix such as a worktree number; a swarm
    without a code owns no branch."""
    from scripts.swarm.naming import TYPES

    code = store.config(slug).code
    branches = re.compile(rf"(?:.+/)?(?:{'|'.join(TYPES.values())})-{re.escape(code)}-\d{{4,}}(?:[-/].*)?")

    def owns(branch: str) -> bool:
        return bool(code) and branches.fullmatch(branch) is not None

    return owns


def port(repository: str) -> SourceControl:
    """Return the source control port for one fleet repository."""
    from scripts.tick.adapters.github import GitHubSourceControl

    return GitHubSourceControl(repository, Path.cwd(), "dev")


def fix_change(url: str) -> ChangeState:
    """Return the state of a fix pull request, read through the source control port."""
    from scripts.swarm import stack

    return stack.change(url, f"cannot read pull request {url}")


def fix_state(kept: tuple[RedisStore, str] | None, url: str) -> ChangeState:
    """Return the state of fix pull request ``url``, kept under the panic keys of the swarm ``kept`` names with its
    store from the read that finds it merged until that panic releases, since a merged pull request never changes; an
    open one, a kept row whose fields no longer match ChangeState, or any read with nothing kept, is read through the
    port."""
    if kept is None:
        return fix_change(url)
    store, slug = kept
    client = _client(store)
    raw = client.hget(_swarm_key(slug, MERGED_FIXES), url)
    row: dict[str, Any] = {} if raw is None else json.loads(raw)
    shaped = set(row) == {field.name for field in fields(ChangeState)}
    if shaped and set(row["reference"]) == {field.name for field in fields(ChangeRef)}:
        return ChangeState(ChangeRef(**row.pop("reference")), **row)
    found = fix_change(url)
    if found.merged:
        client.hset(_swarm_key(slug, MERGED_FIXES), url, json.dumps(asdict(found)))
    return found


def fix_branch(url: str) -> str:
    """Return the head branch of a fix pull request, read through the source control port."""
    return fix_change(url).head_branch


def run_branch(repository: str, resource: str) -> str:
    """Return the head branch of one Actions run or job of ``repository``, ``resource`` being runs/ID or jobs/ID, read
    through the source control port within RUN_READ_SECONDS, short enough that the PreToolUse hook asking it refuses a
    stalled read before its own timeout lets the command through; one without a head branch reads as an empty name,
    and a failed read refuses."""
    from scripts.swarm.stack import PORT_ERRORS, refusal

    path = f"repos/{repository}/actions/{resource}"
    try:
        deadline = Deadline.after(RUN_READ_SECONDS)
        return json.loads(port(repository).download(path, deadline=deadline))["head_branch"] or ""
    except PORT_ERRORS as error:
        raise SwarmError(f"cannot read {path}: {refusal(error)}") from error


def branch_fix(fix: str) -> tuple[str, str]:
    """Return the repository key and the branch a fix branch link https://github.com/OWNER/NAME/tree/BRANCH names,
    or two empty strings for any other fix."""
    from scripts.swarm.stack import GITHUB, repo_key

    parts = fix.removeprefix(GITHUB).split("/", 3)
    if not (fix.startswith(GITHUB) and len(parts) == 4 and parts[2] == "tree" and all(parts)):
        return "", ""
    owner, name, _, branch = parts
    return repo_key(f"{GITHUB}{owner}/{name}"), branch


def check_fix(fix: str) -> None:
    """Refuses, before panic stores it, a fix the cancel sweep could not read or must never spare: anything but a
    pull request or branch link of a panic repository, a branch link to dev or main, and a pull request whose head
    branch is dev or main or that cannot be read."""
    found = FIX_LINK.fullmatch(fix)
    if found is None:
        raise SwarmError(
            f"{fix} is not a fix link: name a pull request https://github.com/OWNER/NAME/pull/NUMBER or a branch "
            "https://github.com/OWNER/NAME/tree/BRANCH"
        )
    repository, number, branch = found.groups()
    if repository not in REPOS:
        raise SwarmError(f"{fix} is outside the panic repositories {', '.join(REPOS)}")
    named = branch if number is None else fix_branch(fix)
    if named in PROTECTED:
        raise SwarmError(f"{fix} names {named}, which panic never spares")


def fixer_links(tasks: Iterable[dict[str, Any]]) -> list[str]:
    """Return, in every panic repository, the branch links of each open promotion fixer task (promo-RUN-N) and
    troubleshooting crew fixer task (crew-STAMP-fix-N) in ``tasks``: the branch the task records, its repository
    branches and the worktree branch of the agent claiming it, which exists from the claim on, before any push; a
    protected branch is never named."""
    from scripts.swarm.stack import GITHUB

    branches: set[str] = set()
    for row in tasks:
        if row.get("state") == DONE or FIXER_TASK.fullmatch(str(row.get("id"))) is None:
            continue
        recorded: dict[str, str] = row.get("repository_branches") or {}
        branches |= {row.get("branch") or "", *recorded.values(), naming.plain(row.get("claimed_by") or "")}
    return sorted(f"{GITHUB}{repo}/tree/{branch}" for repo in REPOS for branch in branches - {"", *PROTECTED})


def keep_fixers(store: RedisStore, slug: str, tasks: Iterable[dict[str, Any]]) -> None:
    """Records the fixer branch links ``fixer_links`` names in the tasks of swarm ``slug`` for every later cancel
    sweep, and drops the record once there are none, so a swarm without an open fixer keeps no record; a record no
    pass writes again lapses after FIXERS_MS, so a stopped or binned swarm stops sparing its branches."""
    key, links = store.key(slug, FIXERS), fixer_links(tasks)
    if links:
        _client(store).set(key, json.dumps(links), px=FIXERS_MS)
    else:
        _client(store).delete(key)


def fixers(store: RedisStore) -> set[str]:
    """Return the fixer branch links the last tick pass of every swarm recorded."""
    found: set[str] = set()
    for slug in cast(list[str], store.slugs()):  # pragma: no mutate
        found.update(json.loads(_client(store).get(store.key(slug, FIXERS)) or "[]"))
    return found


def lab_heads(fixes: Iterable[str], kept: tuple[RedisStore, str]) -> list[str]:
    """Return, sorted, the branch each fix grades on in the lab: a branch link's branch, else the fix pull request's
    head branch, read through fix_state so a merged fix the swarm ``kept`` names already holds is not read again."""
    return sorted({branch_fix(fix)[1] or fix_state(kept, fix).head_branch for fix in fixes})


def lab_engage(store: RedisStore, slug: str, fixes: Iterable[str]) -> str:
    """Tell the swarm's lab to grade the branches of ``fixes`` and of the fixer branches every swarm recorded first, in
    the hotfix class, and to hold the rest of its pull request grading; mark the swarm's panic as holding the lab once
    the lab took both orders. Return the action line; a lab that cannot be told is a line, never a raise."""
    try:
        target = lab_control.lab(store.config(slug).lab_url, os.environ)
        heads = lab_heads((*fixes, *fixers(store)), (store, slug))
        lab_control.priority(target, heads)
        lab_control.pause(target, heads)
    except (lab_control.LabError, subprocess.SubprocessError, OSError, ValueError, KeyError, SwarmError) as error:
        return f"lab not told: {error}"
    _client(store).set(_swarm_key(slug, LAB), target.url)
    return f"lab paused pull request grading except {', '.join(heads) or 'no head'}"


def lab_release(store: RedisStore, slug: str) -> str:
    """Tell the swarm's lab to resume its paused grading and clear the swarm's mark of holding it, only while the
    swarm's panic holds the lab. Return the action line, empty when its panic holds no lab pause; a lab that cannot be
    told is a line, never a raise, and the mark stays."""
    if not _client(store).exists(_swarm_key(slug, LAB)):
        return ""
    try:
        lab_control.resume(lab_control.lab(store.config(slug).lab_url, os.environ))
    except lab_control.LabError as error:
        return f"lab not told: {error}"
    _client(store).delete(_swarm_key(slug, LAB))
    return "lab resumed pull request grading"


def _fix_triggered(row: RepositoryRun, branches: set[str]) -> bool:
    """Return whether the run is a workflow run event run whose run-name names, after its last " for ", one of the
    fix branches as the head branch of the run that triggered it."""
    found = TRIGGER_TITLE.fullmatch(row.title)
    return row.event == WORKFLOW_RUN and found is not None and found[1] in branches


def _spared(row: RepositoryRun, branches: set[str], commits: set[str]) -> bool:
    """Return whether a panic cancel leaves the run alone: finished, the merge queue, a fix, a fix's commit, a
    release tag or a workflow run event run that a fix's run triggered."""
    return (
        row.status not in ACTIVE
        or row.event in (MERGE_QUEUE, RELEASE_EVENT)
        or row.branch in branches
        or row.head_sha in commits
        or RELEASE_TAG.fullmatch(str(row.branch)) is not None
        or _fix_triggered(row, branches)
    )


def _fix_heads(rows: Iterable[RepositoryRun], changes: Iterable[ChangeState], repo: str) -> set[str]:
    """Return the commits a fix in this repository carries onto its base: each merge queue run's head for the fix,
    and the base branch head once the fix is merged, read from rows ordered newest first."""
    found = [change for change in changes if change.reference.repository == repo]
    queues = tuple(f"{QUEUE_BRANCH}{change.base}/pr-{change.reference.number}-" for change in found)
    bases = {change.base for change in found if change.merged}
    heads: dict[str, str] = {}
    queued: set[str] = set()
    for row in rows:
        branch = str(row.branch)
        if row.event == MERGE_QUEUE and branch.startswith(queues):
            queued.add(row.head_sha)
        if row.event == PUSH and branch in bases and row.head_sha:
            heads.setdefault(branch, row.head_sha)
    return (queued | set(heads.values())) - {""}


def _owned(row: RepositoryRun, owns: Callable[[str], bool]) -> bool:
    """Return whether a run belongs to the panicking swarm: its branch is one of the swarm's, or it is a workflow run
    event run whose run-name names one of the swarm's branches after its last " for "."""
    found = TRIGGER_TITLE.fullmatch(row.title)
    return owns(str(row.branch)) or (row.event == WORKFLOW_RUN and found is not None and owns(found[1]))


def cancel_runs(
    fixes: Iterable[str],
    repos: Iterable[str],
    ports: Ports,
    owns: Callable[[str], bool],
    cache: tuple[RedisStore, str] | None = None,
) -> list[str]:
    """Cancels every queued or running Actions run of the panicking swarm, the runs on branches ``owns`` accepts and
    the workflow run event runs they triggered, except the merge queue, release tags, the named fixes, the
    workflow run event runs a fix's run triggered and the runs on a fix's merge queue commit, merge commit or merged
    integration branch head; a fix pull request spares its head branch and a fix branch link its branch only in its
    own repository, and a branch link is never read as a pull request. With ``cache``, the store and the swarm's
    slug, a merged fix pull request is read once and kept, so each sweep reads only the open ones."""
    from scripts.swarm.stack import GITHUB, repo_key

    named = [(fix, *branch_fix(fix)) for fix in fixes]
    changes = [fix_state(cache, fix) for fix, _, branch in named if not branch]
    commits = {change.merge_commit for change in changes if change.merged and change.merge_commit}
    cancelled: list[str] = []
    for repo in repos:
        provider = ports(repo)
        rows = provider.latest_runs(deadline=Deadline.after(TOOL_SECONDS))
        spared = commits | _fix_heads(rows, changes, repo)
        here = repo_key(f"{GITHUB}{repo}")
        kept = {change.head_branch for change in changes if change.reference.repository == repo}
        kept |= {branch for _, key, branch in named if key == here}
        for row in rows:
            if not _owned(row, owns) or _spared(row, kept, spared):
                continue
            provider.cancel_run(row.id, deadline=Deadline.after(TOOL_SECONDS))
            cancelled.append(f"{repo} run {row.id} on {row.branch}")
    return cancelled


def due_cancel(store: RedisStore, slug: str, now_ms: int) -> bool:
    return bool(_client(store).set(_swarm_key(slug, "cancel"), now_ms, nx=True, px=CANCEL_EVERY_MS))


def _swept(fixes: Iterable[str], ports: Ports, store: RedisStore, slug: str) -> list[str]:
    """Return the lines of one cancel sweep of swarm ``slug``'s runs sparing ``fixes`` and the fixer branches every
    swarm recorded: each run it cancelled, or why it failed."""
    try:
        cancelled = cancel_runs((*fixes, *fixers(store)), REPOS, ports, owner(store, slug), (store, slug))
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, SwarmError) as error:
        return [f"panic cancel failed: {error}"]
    return [f"panic cancelled {line}" for line in cancelled]


def _sweep(fixes: Iterable[str], ports: Ports, store: RedisStore, slug: str, report: Callable[[str], None]) -> None:
    """Report each line of one cancel sweep of swarm ``slug``, then free SWEEP, which tick_pass took for it."""
    try:
        for line in _swept(fixes, ports, store, slug):
            report(line)
    finally:
        SWEEP.release()


def tick_pass(
    store: RedisStore, slug: str, ok: bool, now_ms: int, report: Callable[[str], None], ports: Ports = port
) -> list[str]:
    """Counts the swarm's pass and runs its alarm and, while its panic holds, starts the cancel sweep of its agents'
    other CI runs once a minute on its own thread unless an earlier sweep still runs, returning without waiting for
    it; the sweep spares the registered fixes and the fixer branches every swarm recorded, and reports each run it
    cancelled, or why it failed, through ``report``."""
    count_pass(store, slug, ok, now_ms)
    actions = [changed] if (changed := alarm(store, slug, now_ms)) else []
    current = state(store, slug)
    if current is None or not due_cancel(store, slug, now_ms) or not SWEEP.acquire(blocking=False):
        return actions
    thread = Thread(target=_sweep, args=(current.fixes, ports, store, slug, report), name="panic-sweep", daemon=True)
    try:
        thread.start()
    except RuntimeError as error:
        SWEEP.release()
        return [*actions, f"panic cancel failed: {error}"]
    return actions


def settle() -> None:
    """Wait until no cancel sweep runs, so a command process exits only once the sweep its pass started reported."""
    with SWEEP:
        pass
