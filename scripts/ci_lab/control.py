"""The CI lab's control state in its Redis: hotfix priority for named heads, which lapses after PRIORITY_MS, and paused
scheduling classes with the heads each pause excepts, which hold until resume. A head is a pull request head commit or
its head branch. The lease API's control routes write it; the scheduler reads it."""

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from scripts.ci_lab.store import PREFIX

PRIORITY = f"{PREFIX}priority"
PAUSED = f"{PREFIX}paused"
PRIORITY_MS = 6 * 60 * 60 * 1000
HOTFIX = "hotfix"
LOWEST = "-inf"
HIGHEST = "+inf"


def prioritize(client: Any, heads: Iterable[str], now_ms: int) -> list[str]:
    """Mark the heads hotfix until PRIORITY_MS from now, dropping lapsed marks, and return them sorted."""
    named = sorted(set(heads))
    client.zremrangebyscore(PRIORITY, LOWEST, now_ms)
    if named:
        client.zadd(PRIORITY, dict.fromkeys(named, now_ms + PRIORITY_MS))
    return named


def prioritized(client: Any, now_ms: int) -> frozenset[str]:
    """Return the heads whose hotfix mark has not lapsed at ``now_ms``."""
    return frozenset(client.zrangebyscore(PRIORITY, f"({now_ms}", HIGHEST))


def pause(client: Any, name: str, excepted: Iterable[str]) -> list[str]:
    """Hold the class ``name`` but the excepted heads until resume, replacing any earlier pause of it; return the
    excepted heads sorted."""
    kept = sorted(set(excepted))
    client.hset(PAUSED, name, json.dumps(kept))
    return kept


def paused(client: Any) -> dict[str, frozenset[str]]:
    """Return each paused class with the heads its pause excepts."""
    return {name: frozenset(json.loads(raw)) for name, raw in client.hgetall(PAUSED).items()}


def resume(client: Any) -> list[str]:
    """Lift every pause and return the classes it held, sorted."""
    names = sorted(client.hkeys(PAUSED))
    client.delete(PAUSED)
    return names


def holds(pauses: Mapping[str, frozenset[str]], name: str, head: str, lane: str) -> bool:
    """Return whether a pause of class ``name`` holds a head whose commit is ``head`` on ``lane``."""
    excepted = pauses.get(name)
    return excepted is not None and head not in excepted and lane not in excepted


@dataclass(frozen=True)
class Priorities:
    """The scheduler's hotfix port: the heads marked hotfix in the lab's Redis at the clock's now."""

    client: Any
    clock: Callable[[], int]

    def __call__(self) -> frozenset[str]:
        """Return the heads marked hotfix now."""
        return prioritized(self.client, self.clock())
