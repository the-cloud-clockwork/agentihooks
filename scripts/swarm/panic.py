"""Fleet panic mode: a fleet error counter over the last hour, the alarm that engages panic at THRESHOLD errors, the
CI cancellation that frees runners for the named fix, and the business-as-usual level that releases it.

THRESHOLD is three times the busiest measured hour, 19 to 24 tick step failures per hour across the fleet.
"""

import json
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from scripts.swarm.store import RedisStore

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
Runner = Callable[[list[str]], str]


@dataclass(frozen=True)
class Panic:
    since: int
    reason: str
    auto: bool
    fixes: tuple[str, ...]


def _key(store: RedisStore, part: str) -> str:
    return store.key(FLEET, part)


def _emit(slug: str, category: str) -> None:
    from hooks.observability import otel

    otel.add_counter(METRIC, 1, {"swarm": slug, "category": category})


def count_error(store: RedisStore, slug: str, category: str, now_ms: int) -> None:
    """Counts one error in the fleet window and on the OpenTelemetry counter."""
    key = _key(store, "errors")
    store.redis.zadd(key, {f"{now_ms}:{slug}:{category}:{uuid.uuid4().hex}": now_ms})
    store.redis.zremrangebyscore(key, "-inf", now_ms - HOUR_MS - 1)
    _emit(slug, category)


def errors_since(store: RedisStore, since_ms: int) -> int:
    return int(store.redis.zcount(_key(store, "errors"), since_ms, "+inf"))


def errors_by_category(store: RedisStore, since_ms: int) -> dict[str, int]:
    found: dict[str, int] = {}
    for member in store.redis.zrangebyscore(_key(store, "errors"), since_ms, "+inf"):
        category = str(member).split(":")[-2]
        found[category] = found.get(category, 0) + 1
    return found


def count_pass(store: RedisStore, slug: str, ok: bool, now_ms: int) -> None:
    """Records one tick pass and whether every step in it finished without a failure."""
    key = _key(store, "passes")
    store.redis.zadd(key, {f"{now_ms}:{slug}:{int(ok)}:{uuid.uuid4().hex}": now_ms})
    store.redis.zremrangebyscore(key, "-inf", now_ms - WINDOW_MS - 1)


def level(store: RedisStore, now_ms: int) -> float:
    """Business-as-usual level: the share of tick passes in the last WINDOW_MS with no failed step."""
    members = [str(m) for m in store.redis.zrangebyscore(_key(store, "passes"), now_ms - WINDOW_MS, "+inf")]
    if not members:
        return 1.0
    return sum(member.split(":")[-2] == "1" for member in members) / len(members)


def stable(store: RedisStore, now_ms: int) -> bool:
    return level(store, now_ms) >= RELEASE_LEVEL and errors_since(store, now_ms - WINDOW_MS) < RELEASE_ERRORS


def state(store: RedisStore) -> Panic | None:
    raw = store.redis.get(_key(store, "panic"))
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
    store.redis.set(_key(store, "panic"), json.dumps(found.__dict__ | {"fixes": list(found.fixes)}))
    return found


def release(store: RedisStore, now_ms: int) -> bool:
    """Turns panic off only when the fleet is stable and back at RELEASE_LEVEL of business as usual."""
    if not stable(store, now_ms):
        return False
    store.redis.delete(_key(store, "panic"))
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


def gh(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60).stdout


def _branch(url: str, run: Runner) -> str:
    return run(["gh", "pr", "view", url, "--json", "headRefName", "--jq", ".headRefName"]).strip()


def _spared(row: dict[str, str], branches: set[str]) -> bool:
    return row["status"] not in ACTIVE or row["event"] == MERGE_QUEUE or row["headBranch"] in branches


def cancel_runs(fixes: Iterable[str], repos: Iterable[str], run: Runner) -> list[str]:
    """Cancels every queued or running Actions run except the merge queue and the named fix pull requests."""
    branches = {_branch(url, run) for url in fixes}
    cancelled: list[str] = []
    for repo in repos:
        listing = [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--limit",
            "100",
            "--json",
            "databaseId,event,headBranch,status",
        ]
        for row in json.loads(run(listing)):
            if _spared(row, branches):
                continue
            run(["gh", "run", "cancel", str(row["databaseId"]), "--repo", repo])
            cancelled.append(f"{repo} run {row['databaseId']} on {row['headBranch']}")
    return cancelled


def due_cancel(store: RedisStore, now_ms: int) -> bool:
    return bool(store.redis.set(_key(store, "cancel"), now_ms, nx=True, px=CANCEL_EVERY_MS))


def tick_pass(store: RedisStore, slug: str, ok: bool, now_ms: int, run: Runner = gh) -> list[str]:
    """Counts the pass, runs the alarm and, while panic holds, cancels the fleet's other CI runs once a minute."""
    count_pass(store, slug, ok, now_ms)
    actions = [changed] if (changed := alarm(store, now_ms)) else []
    current = state(store)
    if current is None or not due_cancel(store, now_ms):
        return actions
    try:
        cancelled = cancel_runs(current.fixes, REPOS, run)
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        return [*actions, f"panic cancel failed: {error}"]
    return [*actions, *(f"panic cancelled {line}" for line in cancelled)]
