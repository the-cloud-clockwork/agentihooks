"""The lab's soft daily core budget: a core minute counter per UTC day in Redis and the classes it sheds."""

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from scripts.ci_lab.capacity import Load
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
    raise NotImplementedError(runtime)


def day_key(now_ms: int) -> str:
    """The Redis key of the core minute counter for the UTC day holding the instant."""
    raise NotImplementedError(now_ms)


def shed(share: float, fractions: Mapping[str, float]) -> tuple[str, ...]:
    """The classes shed once share of the day's budget is spent."""
    raise NotImplementedError(share, fractions)


def assess(spent_minutes: float, policy: Policy) -> Status:
    """The budget status after spent_minutes of the day's core minutes."""
    raise NotImplementedError(spent_minutes, policy)


def waiting(pending: Mapping[str, int], shed: Collection[str]) -> int:
    """Pending slots of every class but backfill that may lease."""
    raise NotImplementedError(pending, shed)


def slots_wanted(running: Mapping[str, int], pending: Mapping[str, int], shed: Collection[str]) -> int:
    """Slots to ask the autoscaler for."""
    raise NotImplementedError(running, pending, shed)


def admits(name: str, slots: int, free_slots: int, waiting: int, shed: Collection[str]) -> bool:
    """Whether a unit of the class taking slots may lease now."""
    raise NotImplementedError(name, slots, free_slots, waiting, shed)


def observe(load: Load, status: Status) -> Load:
    """The load snapshot carrying the budget fraction and shed classes that /capacity and the metrics show."""
    raise NotImplementedError(load, status)


@dataclass(frozen=True)
class Budget:
    """The day's core minute counter in Redis, read against the policy through an injected millisecond clock."""

    client: Any
    clock: Callable[[], int]
    policy: Policy

    def charge(self, slots: int, seconds: float) -> Status:
        """Add a finished unit's core minutes to today's counter and return the new status."""
        raise NotImplementedError(slots, seconds)

    def spent(self) -> float:
        """Core minutes spent today."""
        raise NotImplementedError

    def status(self) -> Status:
        """The budget status for today."""
        raise NotImplementedError
