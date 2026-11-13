"""Independent queue policy over head observations and estimated pending work."""

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from math import ceil, isfinite
from time import monotonic

from scripts.ci_lab.delivery import CLASSES

__all__ = ["Pending", "order", "supersede", "Debounce"]


@dataclass(frozen=True)
class Pending:
    key: str
    branch: str
    head: str
    priority: str
    estimate: float
    state: str = "pending"


def _positive(value: float) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError("Deficit costs and quantum must be finite and positive")


def _branches(pending: Sequence[Pending]) -> dict[str, deque[Pending]]:
    heads: dict[str, dict[str, list[Pending]]] = {}
    for unit in pending:
        heads.setdefault(unit.branch, {}).setdefault(unit.head, []).append(unit)
    return {
        branch: deque(unit for units in by_head.values() for unit in sorted(units, key=lambda unit: -unit.estimate))
        for branch, by_head in heads.items()
    }


def _rounds(pending: Sequence[Pending], quantum: float) -> list[Pending]:
    queues = _branches(pending)
    deficits = dict.fromkeys(queues, 0.0)
    ordered: list[Pending] = []
    while queues:
        rounds = max(
            1,
            min(ceil((queue[0].estimate - deficits[branch]) / quantum) for branch, queue in queues.items()),
        )
        for branch, queue in tuple(queues.items()):
            deficits[branch] += rounds * quantum
            while queue and queue[0].estimate <= deficits[branch]:
                unit = queue.popleft()
                ordered.append(unit)
                deficits[branch] -= unit.estimate
            if not queue:
                del queues[branch]
    return ordered


def order(pending: Sequence[Pending], quantum: float = 1.0) -> list[Pending]:
    """Spend equal seconds per branch per round, carrying unused deficits.

    Classes follow delivery priority; branches and heads retain first arrival
    order. A head serves its longest estimate first. Empty rounds are skipped.
    """
    _positive(quantum)
    classes: dict[str, list[Pending]] = {}
    for unit in pending:
        _positive(unit.estimate)
        if unit.priority not in CLASSES:
            raise ValueError(f"Unknown priority class: {unit.priority}")
        if unit.state == "pending":
            classes.setdefault(unit.priority, []).append(unit)
    return [unit for priority in CLASSES for unit in _rounds(classes.get(priority, []), quantum)]


def supersede(old_head: Sequence[Pending], new_head: str) -> list[Pending]:
    """Select obsolete pending work from one branch snapshot without mutation."""
    return [unit for unit in old_head if unit.head != new_head and unit.state == "pending"]


@dataclass
class Debounce:
    quiet_window: float = 30.0
    clock: Callable[[], float] = monotonic
    _observed: dict[str, tuple[str, float]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not isfinite(self.quiet_window) or self.quiet_window < 0:
            raise ValueError("The quiet window must be finite and nonnegative")

    def observe(self, branch: str, head: str, now: float | None = None) -> None:
        """Start a fresh quiet window only when the observed head changes."""
        previous = self._observed.get(branch)
        if previous is None or previous[0] != head:
            self._observed[branch] = (head, self.clock() if now is None else now)

    def ready(self, branch: str, now: float | None = None) -> bool:
        observed = self._observed.get(branch)
        if observed is None:
            return False
        current = self.clock() if now is None else now
        return current - observed[1] >= self.quiet_window
