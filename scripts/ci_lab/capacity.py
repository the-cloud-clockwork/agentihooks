"""Capacity document served at GET /capacity, computed from a load snapshot."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import fsum

from scripts.ci_lab.delivery import CLASSES

__all__ = ["Load", "document", "predicted_wait", "utilisation"]


@dataclass(frozen=True)
class Load:
    """Queue and slot state at one instant: pending estimates and running counts per class, and the seconds until
    each running unit frees its slot."""

    pending: Mapping[str, Sequence[float]]
    running: Mapping[str, int]
    slots_total: int
    slots_busy: int
    budget_fraction: float = 1.0
    shed: tuple[str, ...] = ()
    frees_in: Sequence[float] = ()

    @property
    def slots_free(self) -> int:
        """Slots not running a unit, never below zero."""
        return max(self.slots_total - self.slots_busy, 0)


def predicted_wait(estimates: Sequence[float], free_slots: int, frees_in: Sequence[float] = ()) -> float | None:
    """Seconds until the pending estimates drain across the pool, each free slot working from now and each busy slot
    from when its running unit frees it; None when no slot is free and no running unit's free time is known.

    With the ``count`` earliest slots working, the work drains at ``(work + their start times) / count``; every other
    count is no earlier, so the drain is the least of these."""
    work = fsum(estimates)
    if not work:
        return 0.0
    starts = sorted([0.0] * free_slots + [max(at, 0.0) for at in frees_in])
    return min(((work + fsum(starts[:count])) / count for count in range(1, len(starts) + 1)), default=None)


def utilisation(load: Load) -> float:
    """Busy slots over total slots, zero when the pool has no slots."""
    if load.slots_total <= 0:
        return 0.0
    return min(load.slots_busy / load.slots_total, 1.0)


def document(load: Load) -> dict[str, object]:
    """The JSON body of GET /capacity for one load snapshot."""
    free = load.slots_free
    classes = {
        name: {
            "pending": len(load.pending.get(name, ())),
            "running": load.running.get(name, 0),
            "predicted_wait_s": predicted_wait(load.pending.get(name, ()), free, load.frees_in),
        }
        for name in CLASSES
    }
    return {
        "classes": classes,
        "slots": {"total": load.slots_total, "busy": load.slots_busy, "free": free},
        "utilisation": utilisation(load),
        "budget_fraction": load.budget_fraction,
        "shed": [name for name in CLASSES if name in load.shed],
    }
