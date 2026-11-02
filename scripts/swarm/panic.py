"""Fleet panic mode: a fleet error counter over the last hour, the alarm that engages panic at THRESHOLD errors, the
CI cancellation that frees runners for the named fix, and the business-as-usual level that releases it.

THRESHOLD is three times the busiest measured hour, 19 to 24 tick step failures per hour across the fleet.
"""

import json
import math
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.swarm.store import PREFIX, RedisStore, SwarmError
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import SourceControl
from scripts.tick.ports.source_control import RepositoryRun

FLEET = "fleet"
HOUR_MS = 60 * 60 * 1000
WINDOW_MS = 15 * 60 * 1000
THRESHOLD = 60
RELEASE_LEVEL = 0.9
RELEASE_ERRORS = THRESHOLD // 4
CANCEL_EVERY_MS = 60 * 1000
METRIC = "agentihooks.swarm.errors"
MERGE_QUEUE = "merge_group"
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
    """Turns panic on, or adds fixes to the panic already on and makes it manual when a person engaged it."""
    current = state(store)
    found = Panic(
        current.since if current else now_ms,
        current.reason if current else reason,
        auto and (current.auto if current else True),
        tuple(sorted({*(current.fixes if current else ()), *fixes})),
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


def _branch(url: str) -> str:
    """Return the head branch of a fix pull request, read through the source control port."""
    from scripts.swarm import stack

    return stack.change(url, f"cannot read pull request {url}").head_branch


def _spared(row: RepositoryRun, branches: set[str]) -> bool:
    return row.status not in ACTIVE or row.event == MERGE_QUEUE or row.branch in branches


def cancel_runs(fixes: Iterable[str], repos: Iterable[str], ports: Ports) -> list[str]:
    """Cancels every queued or running Actions run except the merge queue and the named fix pull requests."""
    branches = {_branch(url) for url in fixes}
    cancelled: list[str] = []
    for repo in repos:
        provider = ports(repo)
        for row in provider.latest_runs(deadline=Deadline.after(TOOL_SECONDS)):
            if _spared(row, branches):
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
