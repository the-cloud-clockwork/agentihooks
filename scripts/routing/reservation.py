from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hooks.context import account_sessions
from hooks.context.account_sessions import INTERACTIVE_PREFIX
from scripts.routing import master_account

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore

CEILINGS = ((3, 50.0), (2, 60.0), (1, 70.0))
LOWEST = min(limit for _, limit in CEILINGS)
MASTER_NAME = "master@"
AGENT_NAME = "AGENTIHOOKS_AGENT_NAME"


def ceiling(masters_live: int) -> float | None:
    return next((limit for count, limit in CEILINGS if masters_live >= count), None)


def reached(week_used: float | None, masters_live: int) -> bool:
    limit = ceiling(masters_live)
    return limit is not None and week_used is not None and week_used >= limit


def reason(masters_live: int) -> str:
    limit = ceiling(masters_live)
    if limit is None:
        return ""
    noun = "master" if masters_live == 1 else "masters"
    return f"reserved for {masters_live} live {noun}: workers stop at {limit:g}% of the week"


def _session(harness: str, pid: int, proc: Path) -> bool:
    if harness == "codex":
        return account_sessions._is_codex_session(pid, proc)
    return account_sessions._is_interactive_claude(pid, proc)


def interactive_sessions(harness: str, slug: str, proc: Path = account_sessions._PROC) -> int:
    marker = f"{INTERACTIVE_PREFIX}{slug}"
    try:
        pids = [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return 0
    return sum(1 for pid in pids if _session(harness, pid, proc) and marker in account_sessions._env_names(pid, proc))


def master_agents(store: "RedisStore | None", harness: str, slug: str) -> int:
    from scripts.swarm.store import MASTER

    if store is None:
        return 0
    return sum(
        1
        for swarm in store.slugs()
        for agent in store.agents(swarm)
        if agent.lane == MASTER
        and agent.state != "finished"
        and agent.harness == harness
        and agent.account == slug
        and agent.route_kind != "interactive"
    )


def masters_live(
    harness: str, slug: str, store: "RedisStore | None" = None, proc: Path = account_sessions._PROC
) -> int:
    return interactive_sessions(harness, slug, proc) + master_agents(store, harness, slug)


@dataclass(frozen=True)
class Reservation:
    harness: str
    slug: str
    masters: int

    @property
    def ceiling(self) -> float | None:
        return ceiling(self.masters)

    @property
    def reason(self) -> str:
        return reason(self.masters)

    def closes(self, slug: str, week_used: float | None) -> bool:
        return slug == self.slug and reached(week_used, self.masters)


def _store(environ: Mapping[str, str]) -> "RedisStore | None":
    from redis.exceptions import RedisError

    from scripts.swarm.store import RedisStore, redis_client

    try:
        return RedisStore(redis_client(environ))
    except (RedisError, OSError):
        return None


def current(
    harness: str,
    environ: Mapping[str, str],
    store_of: "Callable[[Mapping[str, str]], RedisStore | None] | None" = None,
    proc: Path = account_sessions._PROC,
) -> Reservation | None:
    # redis is imported here: the quota hook loads this module on every tool call.
    from redis.exceptions import RedisError

    try:
        declared = master_account.load(environ).get(harness)
        if declared is None:
            return None
        store = (store_of or _store)(environ)
        return Reservation(harness, declared.slug, masters_live(harness, declared.slug, store, proc))
    except (OSError, RedisError):
        return None


def is_master_session(environ: Mapping[str, str]) -> bool:
    if any(name.startswith(INTERACTIVE_PREFIX) for name in environ):
        return True
    return AGENT_NAME in environ and environ[AGENT_NAME].startswith(MASTER_NAME)


def for_worker(harness: str, environ: Mapping[str, str]) -> Reservation | None:
    return None if is_master_session(environ) else current(harness, environ)
