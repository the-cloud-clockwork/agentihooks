"""Budget and interval of every tick step: p95 from the tick report over 60 passes, intervals from the plan table."""

from math import ceil
from typing import NamedTuple

__all__ = [
    "CRITICAL_S",
    "HEADROOM",
    "JUDGE",
    "MEASURED",
    "SLOW_S",
    "SPAWN_NEEDS",
    "SPAWN_START_S",
    "WIDE_S",
    "Timing",
    "budget_s",
    "timing_of",
]

CRITICAL_S = 60.0
WIDE_S = 120.0
SLOW_S = 600.0
HEADROOM = 1.5
SPAWN_START_S = 40.0
JUDGE = frozenset({"verify", "reap", "launch checks", "track outages", "overrun pass", "quiet pass"})
SPAWN_NEEDS = JUDGE | {"orphans", "preempt"}


class Timing(NamedTuple):
    """The p95 a step measured and the interval it runs at."""

    p95_s: float
    interval_s: float


MEASURED: dict[str, Timing] = {
    "config load": Timing(0.0, CRITICAL_S),
    "stopped branch": Timing(0.0, CRITICAL_S),
    "pane evidence": Timing(1.047, WIDE_S),
    "execution reconcile": Timing(0.442, CRITICAL_S),
    "master move fail over": Timing(0.261, CRITICAL_S),
    "recover master": Timing(0.245, CRITICAL_S),
    "ledger state": Timing(1.748, CRITICAL_S),
    "automated cleanup": Timing(0.0, CRITICAL_S),
    "spawn due": Timing(21.812, CRITICAL_S),
    "exits sweep": Timing(6.067, CRITICAL_S),
    "master start watch": Timing(0.005, CRITICAL_S),
    "launch checks": Timing(0.114, CRITICAL_S),
    "track outages": Timing(3.86, CRITICAL_S),
    "verify": Timing(0.415, CRITICAL_S),
    "reap": Timing(14.511, CRITICAL_S),
    "recover outages": Timing(0.286, CRITICAL_S),
    "strays": Timing(0.27, WIDE_S),
    "failed panes": Timing(0.004, WIDE_S),
    "orphans": Timing(0.021, CRITICAL_S),
    "preempt": Timing(11.749, CRITICAL_S),
    "spawn": Timing(21.812, CRITICAL_S),
    "conversations": Timing(0.412, WIDE_S),
    "session models": Timing(0.459, WIDE_S),
    "handoff transfers": Timing(1.239, CRITICAL_S),
    "master wake": Timing(0.317, CRITICAL_S),
    "retire idle master": Timing(0.02, CRITICAL_S),
    "master stall": Timing(0.134, CRITICAL_S),
    "prompt stall": Timing(0.295, CRITICAL_S),
    "master down": Timing(0.246, CRITICAL_S),
    "tick master": Timing(0.281, CRITICAL_S),
    "master scale": Timing(0.259, CRITICAL_S),
    "seat alarm": Timing(0.006, CRITICAL_S),
    "dispatch seat": Timing(0.255, CRITICAL_S),
    "night watch": Timing(14.078, WIDE_S),
    "master alarm": Timing(0.007, CRITICAL_S),
    "run window": Timing(0.014, CRITICAL_S),
    "verification notices": Timing(0.001, CRITICAL_S),
    "size pass": Timing(0.002, CRITICAL_S),
    "grouping release": Timing(0.002, CRITICAL_S),
    "dispatcher group": Timing(0.546, CRITICAL_S),
    "settle": Timing(0.019, CRITICAL_S),
    "close space": Timing(0.026, CRITICAL_S),
    "collect acks": Timing(4.61, CRITICAL_S),
    "codex hook order": Timing(0.0, CRITICAL_S),
    "rank pass": Timing(4.488, CRITICAL_S),
    "bottleneck remedy": Timing(0.04, CRITICAL_S),
    "inbox relay": Timing(0.332, CRITICAL_S),
    "launch slots": Timing(0.116, CRITICAL_S),
    "outage probes": Timing(0.0, CRITICAL_S),
    "hive ledger": Timing(0.0, WIDE_S),
    "ledger health": Timing(0.0, WIDE_S),
    "host pressure": Timing(0.145, WIDE_S),
    "ledger watchdog": Timing(0.041, WIDE_S),
    "ledger probe": Timing(4.476, WIDE_S),
    "prompt backlog": Timing(0.007, WIDE_S),
    "command runner": Timing(0.256, CRITICAL_S),
    "urgent hold": Timing(0.088, CRITICAL_S),
    "planning and phase pass": Timing(7.626, CRITICAL_S),
    "refresh job": Timing(0.012, SLOW_S),
    "intent check": Timing(5.331, CRITICAL_S),
    "reboot pass": Timing(0.006, CRITICAL_S),
    "tick call": Timing(107.841, CRITICAL_S),
    "outbox migration": Timing(0.006, CRITICAL_S),
    "page relay": Timing(0.987, CRITICAL_S),
    "tick view": Timing(0.98, CRITICAL_S),
    "ledger event pass": Timing(0.16, CRITICAL_S),
    "done recheck": Timing(0.088, CRITICAL_S),
    "progress checks": Timing(0.056, CRITICAL_S),
    "waits end": Timing(1.646, CRITICAL_S),
    "merge pass": Timing(0.082, CRITICAL_S),
    "stall watch": Timing(11.963, CRITICAL_S),
    "overrun pass": Timing(0.416, CRITICAL_S),
    "quiet pass": Timing(0.637, CRITICAL_S),
    "doc report": Timing(0.097, SLOW_S),
    "priorities sweep": Timing(0.011, CRITICAL_S),
    "health findings": Timing(5.159, CRITICAL_S),
    "findings pass": Timing(0.078, CRITICAL_S),
    "metrics record": Timing(18.905, SLOW_S),
    "bottleneck status": Timing(0.023, WIDE_S),
    "inbox wake": Timing(0.34, CRITICAL_S),
    "snapshot job": Timing(0.009, SLOW_S),
    "last tick": Timing(0.005, CRITICAL_S),
    "heartbeat": Timing(0.0, CRITICAL_S),
    "status publish": Timing(0.0, CRITICAL_S),
}


def budget_s(p95_s: float) -> float:
    """Return the budget for a step that measured ``p95_s``: the p95 with headroom, whole seconds, at least one."""
    return float(max(1, ceil(p95_s * HEADROOM)))


def timing_of(name: str) -> tuple[float, float]:
    """Return the budget and interval step ``name`` declares, refusing a step the table does not declare."""
    if name not in MEASURED:
        raise ValueError(f"Undeclared step timing: {name}")
    measured = MEASURED[name]
    return budget_s(measured.p95_s), measured.interval_s
