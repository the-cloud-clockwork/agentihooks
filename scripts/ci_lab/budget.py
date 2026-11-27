"""The lab's soft daily core budget: a core minute counter per UTC day in Redis and the classes it sheds."""

import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from typing import Any

from scripts.ci_lab.capacity import Load
from scripts.ci_lab.delivery import CLASSES, SHEDDABLE
from scripts.ci_lab.store import PREFIX

__all__ = [
    "BACKFILL",
    "KEY",
    "TTL_S",
    "Budget",
    "Policy",
    "Status",
    "admits",
    "assess",
    "day_key",
    "observe",
    "policy",
    "shed",
    "slots_wanted",
    "waiting",
]

KEY = f"{PREFIX}budget:"
TTL_S = 2 * 24 * 60 * 60
BACKFILL = "backfill"


@dataclass(frozen=True)
class Policy:
    """The day's core minutes and the spent share of them at which each sheddable class sheds."""

    core_minutes_per_day: float
    fractions: Mapping[str, float]


@dataclass(frozen=True)
class Status:
    """The fraction of the day's budget left and the classes shed, in shed order."""

    fraction: float
    shed: tuple[str, ...]


def policy(runtime: Mapping[str, Any]) -> Policy:
    """The budget policy of a validated scheduler runtime config."""
    budget = runtime["budget"]
    return Policy(budget["coreMinutesPerDay"], budget["shed"])


def day_key(now_ms: int) -> str:
    """The Redis key of the core minute counter for the UTC day holding the instant."""
    return KEY + time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))


def shed(share: float, fractions: Mapping[str, float]) -> tuple[str, ...]:
    """The classes shed once share of the day's budget is spent.

    A class sheds when the share reaches its fraction, and every class before it in the shed order sheds with it, so
    backfill always sheds first; a class without a fraction sheds only behind a later one. Classes outside the shed
    order, hotfix and pull_request, never shed.
    """
    depth = 0
    for index, name in enumerate(SHEDDABLE, start=1):
        if name in fractions and share >= fractions[name]:
            depth = index
    return SHEDDABLE[:depth]


def assess(spent_minutes: float, budget_policy: Policy) -> Status:
    """The budget status after spent_minutes of the day's core minutes."""
    share = spent_minutes / budget_policy.core_minutes_per_day
    return Status(max(1.0 - share, 0.0), shed(share, budget_policy.fractions))


def waiting(pending: Mapping[str, int], shed_classes: Collection[str]) -> int:
    """Pending slots of every class but backfill that may lease."""
    return sum(pending.get(name, 0) for name in CLASSES if name != BACKFILL and name not in shed_classes)


def slots_wanted(running: Mapping[str, int], pending: Mapping[str, int], shed_classes: Collection[str]) -> int:
    """Slots to ask the autoscaler for: running slots of every class but backfill plus the waiting slots.

    Backfill never counts, so it takes free slots only; a shed class keeps its running slots but not its pending ones.
    """
    return sum(running.get(name, 0) for name in CLASSES if name != BACKFILL) + waiting(pending, shed_classes)


def admits(name: str, slots: int, free_slots: int, waiting_slots: int, shed_classes: Collection[str]) -> bool:
    """Whether a unit of the class taking slots may lease now.

    A shed class never leases; backfill leases only into the free slots left after waiting_slots, the slots that lease
    before it. The scheduler admits units into one queue that leases the most urgent class first and in order inside a
    class, backfill last, so it passes every queued slot, shed classes and backfill included, not the waiting
    function's count.
    """
    if name in shed_classes:
        return False
    return name != BACKFILL or slots <= free_slots - waiting_slots


def observe(load: Load, status: Status) -> Load:
    """The load snapshot carrying the budget fraction and shed classes that /capacity and the metrics show."""
    return replace(load, budget_fraction=status.fraction, shed=status.shed)


@dataclass(frozen=True)
class Budget:
    """The day's core minute counter in Redis, read against the policy through an injected millisecond clock."""

    client: Any
    clock: Callable[[], int]
    policy: Policy

    def charge(self, slots: int, seconds: float) -> Status:
        """Add a finished unit's core minutes, slots times seconds, to today's counter and return the new status."""
        key = day_key(self.clock())
        pipe = self.client.pipeline()
        pipe.incrbyfloat(key, slots * seconds / 60)
        pipe.expire(key, TTL_S)
        spent, _ = pipe.execute()
        return assess(float(spent), self.policy)

    def spent(self) -> float:
        """Core minutes spent today."""
        value = self.client.get(day_key(self.clock()))
        return 0.0 if value is None else float(value)

    def status(self) -> Status:
        """The budget status for today."""
        return assess(self.spent(), self.policy)
