"""Budget and interval of every tick step: a floor per step, raised each pass to the live p95 of its last 60 walls."""

from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from math import ceil
from types import MappingProxyType
from typing import Any, NamedTuple

from scripts.swarm.timing import WALLS, WINDOW
from scripts.tick.pool import SPAWN_RESERVE_S

__all__ = [
    "CRITICAL_S",
    "FLOORS",
    "HEADROOM",
    "JUDGE",
    "SLOW_S",
    "SPAWN_NEEDS",
    "SPAWN_START_S",
    "WIDE_S",
    "WINDOW",
    "Timing",
    "budget_of",
    "budget_s",
    "floor_of",
    "floored",
    "resolve",
    "timing_of",
]

CRITICAL_S = 60.0
WIDE_S = 120.0
SLOW_S = 600.0
HEADROOM = 1.5
SPAWN_START_S = CRITICAL_S - SPAWN_RESERVE_S
LIGHT_S = 2.0
REBUILT = "tick:walls-rebuilt"
_LEDGER_READ_S = 3.0
JUDGE = frozenset({"verify", "reap", "launch checks", "track outages", "overrun pass", "quiet pass"})
SPAWN_NEEDS = JUDGE | {"orphans", "preempt"}
LIVE: ContextVar[Mapping[str, float]] = ContextVar("tick_live_budgets", default=MappingProxyType({}))


class Timing(NamedTuple):
    """The floor a step's budget never goes under and the interval it runs at."""

    floor_s: float
    interval_s: float


FLOORS: dict[str, Timing] = {
    "config load": Timing(LIGHT_S, CRITICAL_S),
    "stopped branch": Timing(LIGHT_S, CRITICAL_S),
    "pane evidence": Timing(LIGHT_S, WIDE_S),
    "execution reconcile": Timing(LIGHT_S, CRITICAL_S),
    "master move fail over": Timing(LIGHT_S, CRITICAL_S),
    "recover master": Timing(LIGHT_S, CRITICAL_S),
    "ledger state": Timing(_LEDGER_READ_S, CRITICAL_S),
    "automated cleanup": Timing(LIGHT_S, CRITICAL_S),
    "spawn due": Timing(33.0, CRITICAL_S),
    "exits sweep": Timing(10.0, CRITICAL_S),
    "master start watch": Timing(LIGHT_S, CRITICAL_S),
    "launch checks": Timing(LIGHT_S, CRITICAL_S),
    "track outages": Timing(6.0, CRITICAL_S),
    "verify": Timing(LIGHT_S, CRITICAL_S),
    "reap": Timing(22.0, CRITICAL_S),
    "recover outages": Timing(LIGHT_S, CRITICAL_S),
    "strays": Timing(LIGHT_S, WIDE_S),
    "failed panes": Timing(LIGHT_S, WIDE_S),
    "orphans": Timing(LIGHT_S, CRITICAL_S),
    "preempt": Timing(18.0, CRITICAL_S),
    "spawn": Timing(33.0, CRITICAL_S),
    "conversations": Timing(LIGHT_S, WIDE_S),
    "session models": Timing(LIGHT_S, WIDE_S),
    "handoff transfers": Timing(LIGHT_S, CRITICAL_S),
    "master wake": Timing(LIGHT_S, CRITICAL_S),
    "retire idle master": Timing(LIGHT_S, CRITICAL_S),
    "master stall": Timing(LIGHT_S, CRITICAL_S),
    "prompt stall": Timing(LIGHT_S, CRITICAL_S),
    "master down": Timing(LIGHT_S, CRITICAL_S),
    "tick master": Timing(LIGHT_S, CRITICAL_S),
    "master scale": Timing(LIGHT_S, CRITICAL_S),
    "seat alarm": Timing(LIGHT_S, CRITICAL_S),
    "dispatch seat": Timing(LIGHT_S, CRITICAL_S),
    "night watch": Timing(22.0, WIDE_S),
    "master alarm": Timing(LIGHT_S, CRITICAL_S),
    "spawn hold clear": Timing(LIGHT_S, CRITICAL_S),
    "pass budget": Timing(LIGHT_S, CRITICAL_S),
    "sleeping": Timing(LIGHT_S, CRITICAL_S),
    "drained resume": Timing(LIGHT_S, CRITICAL_S),
    "dev red reopen": Timing(LIGHT_S, CRITICAL_S),
    "prerequisite reopen": Timing(LIGHT_S, CRITICAL_S),
    "promotion refresh": Timing(LIGHT_S, CRITICAL_S),
    "release alert": Timing(LIGHT_S, CRITICAL_S),
    "fleet red notice": Timing(30.0, CRITICAL_S),
    "quota notice": Timing(LIGHT_S, CRITICAL_S),
    "lost runner": Timing(LIGHT_S, CRITICAL_S),
    "time left": Timing(LIGHT_S, CRITICAL_S),
    "run window": Timing(LIGHT_S, CRITICAL_S),
    "verification notices": Timing(LIGHT_S, CRITICAL_S),
    "size pass": Timing(LIGHT_S, CRITICAL_S),
    "grouping release": Timing(LIGHT_S, CRITICAL_S),
    "dispatcher group": Timing(LIGHT_S, CRITICAL_S),
    "settle": Timing(LIGHT_S, CRITICAL_S),
    "close space": Timing(LIGHT_S, CRITICAL_S),
    "collect acks": Timing(7.0, CRITICAL_S),
    "codex hook order": Timing(LIGHT_S, CRITICAL_S),
    "rank pass": Timing(7.0, CRITICAL_S),
    "bottleneck remedy": Timing(LIGHT_S, CRITICAL_S),
    "inbox relay": Timing(LIGHT_S, CRITICAL_S),
    "launch slots": Timing(LIGHT_S, CRITICAL_S),
    "outage probes": Timing(LIGHT_S, CRITICAL_S),
    "hive ledger": Timing(LIGHT_S, WIDE_S),
    "ledger health": Timing(LIGHT_S, CRITICAL_S),
    "host pressure": Timing(LIGHT_S, WIDE_S),
    "ledger watchdog": Timing(LIGHT_S, WIDE_S),
    "ledger probe": Timing(7.0, WIDE_S),
    "prompt backlog": Timing(LIGHT_S, WIDE_S),
    "command runner": Timing(LIGHT_S, CRITICAL_S),
    "urgent hold": Timing(LIGHT_S, CRITICAL_S),
    "dependency audit": Timing(LIGHT_S, CRITICAL_S),
    "planning and phase pass": Timing(12.0, CRITICAL_S),
    "planning after phase": Timing(12.0, CRITICAL_S),
    "refresh job": Timing(LIGHT_S, SLOW_S),
    "intent check": Timing(8.0, CRITICAL_S),
    "reboot pass": Timing(LIGHT_S, CRITICAL_S),
    "outbox migration": Timing(LIGHT_S, CRITICAL_S),
    "page relay": Timing(LIGHT_S, CRITICAL_S),
    "tick view": Timing(_LEDGER_READ_S, CRITICAL_S),
    "ledger event pass": Timing(LIGHT_S, CRITICAL_S),
    "done recheck": Timing(LIGHT_S, CRITICAL_S),
    "progress checks": Timing(LIGHT_S, CRITICAL_S),
    "waits end": Timing(3.0, CRITICAL_S),
    "merge pass": Timing(LIGHT_S, CRITICAL_S),
    "stall watch": Timing(18.0, CRITICAL_S),
    "overrun pass": Timing(LIGHT_S, CRITICAL_S),
    "quiet pass": Timing(LIGHT_S, CRITICAL_S),
    "doc report": Timing(LIGHT_S, SLOW_S),
    "priorities sweep": Timing(LIGHT_S, CRITICAL_S),
    "health findings": Timing(8.0, CRITICAL_S),
    "findings pass": Timing(LIGHT_S, CRITICAL_S),
    "metrics record": Timing(29.0, SLOW_S),
    "bottleneck status": Timing(LIGHT_S, WIDE_S),
    "inbox wake": Timing(LIGHT_S, CRITICAL_S),
    "snapshot job": Timing(LIGHT_S, CRITICAL_S),
    "last tick": Timing(LIGHT_S, CRITICAL_S),
    "heartbeat": Timing(LIGHT_S, CRITICAL_S),
    "status publish": Timing(LIGHT_S, CRITICAL_S),
    "hive sweep": Timing(LIGHT_S, SLOW_S),
    "workflow runner": Timing(LIGHT_S, CRITICAL_S),
    "doc items map": Timing(LIGHT_S, CRITICAL_S),
    "step starvation": Timing(LIGHT_S, CRITICAL_S),
    "knowledge warm": Timing(LIGHT_S, CRITICAL_S),
    "progress heads": Timing(LIGHT_S, CRITICAL_S),
    "lab capacity": Timing(_LEDGER_READ_S, CRITICAL_S),
}


def budget_s(p95_s: float) -> float:
    """Return the budget for a step that measured ``p95_s``: the p95 with headroom, whole seconds, at least one."""
    return float(max(1, ceil(p95_s * HEADROOM)))


def _declared(name: str) -> Timing:
    """Return the floor and interval step ``name`` declares, refusing a step the table does not declare."""
    if name not in FLOORS:
        raise ValueError(f"Undeclared step timing: {name}")
    return FLOORS[name]


def floor_of(name: str) -> float:
    """Return the floor step ``name`` declares: its budget until it has a full window of walls."""
    return _declared(name).floor_s


def budget_of(name: str) -> float:
    """Return step ``name``'s budget this pass: its live budget when that is above its floor, else the floor."""
    return floored(name, floor_of(name))


def floored(name: str, floor_s: float) -> float:
    """Return step ``name``'s live budget this pass when that is above ``floor_s``, else ``floor_s``."""
    return max(floor_s, LIVE.get().get(name, 0.0))


def timing_of(name: str) -> tuple[float, float]:
    """Return the budget and interval step ``name`` has this pass, refusing a step the table does not declare."""
    return budget_of(name), _declared(name).interval_s


def live_budget(walls: list[float]) -> float | None:
    """Return the p95 of ``walls`` with headroom when they fill the window, else None."""
    if len(walls) < WINDOW:
        return None
    return budget_s(sorted(walls)[ceil(len(walls) * 0.95) - 1])


def resolve(store: Any, slug: str) -> dict[str, float]:
    """Set this pass's live budgets of swarm ``slug`` from each step's walls window and return them."""
    live = {name: found for name, walls in _walls(store, slug).items() if (found := live_budget(walls)) is not None}
    LIVE.set(MappingProxyType(live))
    return live


def _walls(store: Any, slug: str) -> dict[str, list[float]]:
    """Return the last walls of every declared step, rebuilding missing windows from the steps stream at most once
    every slow interval, empty when Redis fails."""
    from redis.exceptions import RedisError

    names = list(FLOORS)
    try:
        pipe = store.redis.pipeline(transaction=False)
        for name in names:
            pipe.lrange(store.key(slug, WALLS + name), 0, WINDOW - 1)
        found = {name: [float(wall) for wall in walls] for name, walls in zip(names, pipe.execute(), strict=True)}
        missing = {name for name, walls in found.items() if not walls}
        if missing and store.redis.set(store.key(slug, REBUILT), "1", nx=True, ex=int(SLOW_S)):
            found.update(_rebuild(store, slug, missing))
    except RedisError:
        return {}
    return found


def _rebuild(store: Any, slug: str, missing: set[str]) -> dict[str, list[float]]:
    """Rebuild the walls window of each ``missing`` step from its newest budgeted runs in the steps stream."""
    from scripts.swarm import tick_report
    from scripts.swarm.timing import HISTORY_LIMIT

    rebuilt = _newest(tick_report.history(store, slug, "tick:steps", HISTORY_LIMIT), missing)
    pipe = store.redis.pipeline(transaction=False)
    for name, walls in rebuilt.items():
        pipe.rpush(store.key(slug, WALLS + name), *map(repr, walls))
    pipe.execute()
    return rebuilt


def _newest(records: Iterable[Mapping[str, Any]], names: set[str]) -> dict[str, list[float]]:
    """Return up to a window of walls per step in ``names`` from budgeted step ``records``, newest first."""
    walls: dict[str, list[float]] = {}
    for record in records:
        name = record.get("step")
        if name in names and "budget_s" in record and "wall_s" in record:
            kept = walls.setdefault(name, [])
            if len(kept) < WINDOW:
                kept.append(float(record["wall_s"]))
    return walls
