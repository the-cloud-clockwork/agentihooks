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
JUDGE = frozenset({"verify", "reap", "launch checks", "track outages", "overrun", "quiet"})
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
    "overrun": Timing(0.416, CRITICAL_S),
    "quiet": Timing(0.637, CRITICAL_S),
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
