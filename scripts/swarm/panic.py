"""Fleet panic mode: a fleet error counter over the last hour, the alarm that engages panic at THRESHOLD errors, the
CI cancellation that frees runners for the named fixes and every swarm's open fixer tasks, and the business-as-usual
level that releases it.

THRESHOLD is three times the busiest measured hour, 19 to 24 tick step failures per hour across the fleet.

A step or a pass that merely runs long, up to the golden pass duration (`scripts.tick.engine.GOLDEN_PASS_S`),
never reaches either counter here: `count_error` only takes a category a caller chose to report, and the one
caller wired to a tick step (`scripts.swarm.timing.step`) reports only on a raised exception, never on a step
recorded late; `count_pass` only takes the ``ok`` a caller computed, and that caller
(`scripts.swarm.health.spawn_stall.watch`) sets it from whether any step raised, never from the pass's wall time.
"""

import json
import math
import re
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from threading import Lock, Thread
from typing import Any, cast

from scripts.swarm import naming
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
MERGED_FIXES = "merged-fixes"
FIXER_TASK = re.compile(r"promo-[0-9]+-[0-9]+|crew-[0-9]+-fix-[0-9]+")
FIXERS = "panic-fixers"
FIXERS_MS = 15 * 60 * 1000
DONE = "done"
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


def _client(store: RedisStore) -> Any:
    return getattr(store, "redis")


def _emit(slug: str, category: str) -> None:
    from hooks.observability import otel

    otel.add_counter(METRIC, 1, {"swarm": slug, "category": category})


def count_error(store: RedisStore, slug: str, category: str, now_ms: int) -> None:
    """Counts one error in the fleet window and on the OpenTelemetry counter."""
    key = _key("errors")
    _client(store).zadd(key, {f"{category}|{now_ms}|{uuid.uuid4().hex}": now_ms})
    _client(store).zremrangebyscore(key, 0, now_ms - HOUR_MS - 1)
    _emit(slug, category)


def errors_since(store: RedisStore, since_ms: int) -> int:
    return int(_client(store).zcount(_key("errors"), since_ms, math.inf))


def errors_by_category(store: RedisStore, since_ms: int) -> dict[str, int]:
    found: dict[str, int] = {}
    for member in _client(store).zrangebyscore(_key("errors"), since_ms, math.inf):
        category = str(member).split("|")[0]
        found[category] = found.get(category, 0) + 1
    return found


def count_pass(store: RedisStore, ok: bool, now_ms: int) -> None:
    """Records one tick pass and whether every step in it finished without a failure."""
    key = _key("passes-ok" if ok else "passes-failed")
    _client(store).zadd(key, {f"{now_ms}|{uuid.uuid4().hex}": now_ms})
    _client(store).zremrangebyscore(key, 0, now_ms - WINDOW_MS - 1)


def level(store: RedisStore, now_ms: int) -> float:
    """Business-as-usual level: the share of tick passes in the last WINDOW_MS with no failed step."""
    since = now_ms - WINDOW_MS
    ok = int(_client(store).zcount(_key("passes-ok"), since, math.inf))
    failed = int(_client(store).zcount(_key("passes-failed"), since, math.inf))
    return ok / (ok + failed) if ok + failed else 1.0


def stable(store: RedisStore, now_ms: int) -> bool:
    return level(store, now_ms) >= RELEASE_LEVEL and errors_since(store, now_ms - WINDOW_MS) < RELEASE_ERRORS


def state(store: RedisStore) -> Panic | None:
    raw = _client(store).get(_key("panic"))
    if raw is None:
        return None
    row = json.loads(raw)
    return Panic(row["since"], row["reason"], row["auto"], tuple(row["fixes"]))


def engage(store: RedisStore, reason: str, auto: bool, fixes: Iterable[str], now_ms: int) -> Panic:
    """Turns panic on, or adds fixes to the panic already on; a person pressing on with no fix makes it manual."""
    current = state(store)
    added = tuple(fixes)
    found = Panic(
        current.since if current else now_ms,
        current.reason if current else reason,
        (current.auto and (auto or bool(added))) if current else auto,
        tuple(sorted({*(current.fixes if current else ()), *added})),
    )
    _client(store).set(_key("panic"), json.dumps(found.__dict__))
    return found


def release(store: RedisStore, now_ms: int) -> bool:
    """Turns panic off only when the fleet is stable and back at RELEASE_LEVEL of business as usual."""
    if not stable(store, now_ms):
        return False
    _client(store).delete(_key("panic"), _key(MERGED_FIXES))
    return True


def alarm(store: RedisStore, now_ms: int) -> str:
    """Engages panic at THRESHOLD errors in the last hour; releases an automatic panic once stable."""
    current = state(store)
    if current is None:
        count = errors_since(store, now_ms - HOUR_MS)
        if count < THRESHOLD:
            return ""
        engage(store, f"{count} errors in the last hour", True, (), now_ms)
        return f"panic engaged: {count} errors in the last hour"
    if current.auto and release(store, now_ms):
        return "panic released: the fleet is back at business as usual"
    return ""


def port(repository: str) -> SourceControl:
    """Return the source control port for one fleet repository."""
    from scripts.tick.adapters.github import GitHubSourceControl

    return GitHubSourceControl(repository, Path.cwd(), "dev")


def fix_change(url: str) -> ChangeState:
    """Return the state of a fix pull request, read through the source control port."""
    from scripts.swarm import stack

    return stack.change(url, f"cannot read pull request {url}")


def fix_state(store: RedisStore | None, url: str) -> ChangeState:
    """Return the state of fix pull request ``url``, kept under the fleet panic keys from the read that finds it
    merged until panic releases, since a merged pull request never changes; an open one, a kept row whose fields no
    longer match ChangeState, or any read without a store, is read through the port."""
    if store is None:
        return fix_change(url)
    client = _client(store)
    raw = client.hget(_key(MERGED_FIXES), url)
    row: dict[str, Any] = {} if raw is None else json.loads(raw)
    shaped = set(row) == {field.name for field in fields(ChangeState)}
    if shaped and set(row["reference"]) == {field.name for field in fields(ChangeRef)}:
        return ChangeState(ChangeRef(**row.pop("reference")), **row)
    found = fix_change(url)
    if found.merged:
        client.hset(_key(MERGED_FIXES), url, json.dumps(asdict(found)))
    return found


def fix_branch(url: str) -> str:
    """Return the head branch of a fix pull request, read through the source control port."""
    return fix_change(url).head_branch


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


def cancel_runs(fixes: Iterable[str], repos: Iterable[str], ports: Ports, store: RedisStore | None = None) -> list[str]:
    """Cancels every queued or running Actions run except the merge queue, release tags, the named fixes, the
    workflow run event runs a fix's run triggered and the runs on a fix's merge queue commit, merge commit or merged
    integration branch head; a fix pull request spares its head branch and a fix branch link its branch only in its
    own repository, and a branch link is never read as a pull request. With ``store`` a merged fix pull request is
    read once and kept, so each sweep reads only the open ones."""
    from scripts.swarm.stack import GITHUB, repo_key

    named = [(fix, *branch_fix(fix)) for fix in fixes]
    changes = [fix_state(store, fix) for fix, _, branch in named if not branch]
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
            if _spared(row, kept, spared):
                continue
            provider.cancel_run(row.id, deadline=Deadline.after(TOOL_SECONDS))
            cancelled.append(f"{repo} run {row.id} on {row.branch}")
    return cancelled


def due_cancel(store: RedisStore, now_ms: int) -> bool:
    return bool(_client(store).set(_key("cancel"), now_ms, nx=True, px=CANCEL_EVERY_MS))


def _swept(fixes: Iterable[str], ports: Ports, store: RedisStore) -> list[str]:
    """Return the lines of one cancel sweep sparing ``fixes`` and the fixer branches every swarm recorded: each run
    it cancelled, or why it failed."""
    try:
        return [f"panic cancelled {line}" for line in cancel_runs((*fixes, *fixers(store)), REPOS, ports, store)]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, SwarmError) as error:
        return [f"panic cancel failed: {error}"]


def _sweep(fixes: Iterable[str], ports: Ports, store: RedisStore, report: Callable[[str], None]) -> None:
    """Report each line of one cancel sweep, then free SWEEP, which tick_pass took for it."""
    try:
        for line in _swept(fixes, ports, store):
            report(line)
    finally:
        SWEEP.release()


def tick_pass(
    store: RedisStore, ok: bool, now_ms: int, report: Callable[[str], None], ports: Ports = port
) -> list[str]:
    """Counts the pass and runs the alarm and, while panic holds, starts the cancel sweep once a minute on its own
    thread unless an earlier sweep still runs, returning without waiting for it; the sweep reports each run it
    cancelled, or why it failed, through ``report``."""
    count_pass(store, ok, now_ms)
    actions = [changed] if (changed := alarm(store, now_ms)) else []
    current = state(store)
    if current is None or not due_cancel(store, now_ms) or not SWEEP.acquire(blocking=False):
        return actions
    thread = Thread(target=_sweep, args=(current.fixes, ports, store, report), name="panic-sweep", daemon=True)
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
