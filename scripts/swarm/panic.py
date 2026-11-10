"""Fleet panic mode: a fleet error counter over the last hour, the alarm that engages panic at THRESHOLD errors, the
CI cancellation that frees runners for the named fix, and the business-as-usual level that releases it.

THRESHOLD is three times the busiest measured hour, 19 to 24 tick step failures per hour across the fleet.

A step or a pass that merely runs long, up to the golden pass duration (`GOLDEN_PASS_S`), never reaches either
counter here: `count_error` only takes a category a caller chose to report, and the one caller wired to a tick
step (`scripts.swarm.timing.step`) reports only on a raised exception, never on a step recorded late; `count_pass`
only takes the ``ok`` a caller computed, and that caller (`scripts.swarm.health.spawn_stall.watch`) sets it from
whether any step raised, never from the pass's wall time.
"""

import json
import math
import re
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.swarm.store import PREFIX, RedisStore, SwarmError
from scripts.tick.deadline import Deadline
from scripts.tick.engine import GOLDEN_PASS_S
from scripts.tick.ports.contracts import SourceControl
from scripts.tick.ports.source_control import ChangeState, RepositoryRun

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
REPOS = (
    "the-cloud-clockwork/agentihooks",
    "the-cloud-clockwork/agentihooks-bundle",
    "the-cloud-clockwork/antoncore",
)
ACTIVE = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
TOOL_SECONDS = 60.0
Ports = Callable[[str], SourceControl]


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


def within_golden_pass(wall_s: float) -> bool:
    """Return whether a step or pass wall time sits within the golden pass duration, never a panic signal on its own."""
    return wall_s <= GOLDEN_PASS_S


def count_error(store: RedisStore, slug: str, category: str, now_ms: int, wall_s: float | None = None) -> None:
    """Counts one error in the fleet window and on the OpenTelemetry counter, unless ``wall_s`` is given and sits
    within the golden pass duration, in which case it is no error."""
    if wall_s is not None and within_golden_pass(wall_s):
        return
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
    _client(store).delete(_key("panic"))
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


def fix_branch(url: str) -> str:
    """Return the head branch of a fix pull request, read through the source control port."""
    return fix_change(url).head_branch


def _spared(row: RepositoryRun, branches: set[str], commits: set[str]) -> bool:
    """Return whether a panic cancel leaves the run alone: finished, the merge queue, a fix, a fix's commit or a
    release tag."""
    return (
        row.status not in ACTIVE
        or row.event in (MERGE_QUEUE, RELEASE_EVENT)
        or row.branch in branches
        or row.head_sha in commits
        or RELEASE_TAG.fullmatch(str(row.branch)) is not None
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


def cancel_runs(fixes: Iterable[str], repos: Iterable[str], ports: Ports) -> list[str]:
    """Cancels every queued or running Actions run except the merge queue, release tags, the named fixes and the
    runs on a fix's merge queue commit, merge commit or merged integration branch head."""
    changes = [fix_change(url) for url in fixes]
    branches = {change.head_branch for change in changes}
    commits = {change.merge_commit for change in changes if change.merged and change.merge_commit}
    cancelled: list[str] = []
    for repo in repos:
        provider = ports(repo)
        rows = provider.latest_runs(deadline=Deadline.after(TOOL_SECONDS))
        spared = commits | _fix_heads(rows, changes, repo)
        for row in rows:
            if _spared(row, branches, spared):
                continue
            provider.cancel_run(row.id, deadline=Deadline.after(TOOL_SECONDS))
            cancelled.append(f"{repo} run {row.id} on {row.branch}")
    return cancelled


def due_cancel(store: RedisStore, now_ms: int) -> bool:
    return bool(_client(store).set(_key("cancel"), now_ms, nx=True, px=CANCEL_EVERY_MS))


def tick_pass(store: RedisStore, ok: bool, now_ms: int, ports: Ports = port) -> list[str]:
    """Counts the pass, runs the alarm and, while panic holds, cancels the fleet's other CI runs once a minute."""
    count_pass(store, ok, now_ms)
    actions = [changed] if (changed := alarm(store, now_ms)) else []
    current = state(store)
    if current is None or not due_cancel(store, now_ms):
        return actions
    try:
        cancelled = cancel_runs(current.fixes, REPOS, ports)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, SwarmError) as error:
        return [*actions, f"panic cancel failed: {error}"]
    return [*actions, *(f"panic cancelled {line}" for line in cancelled)]
