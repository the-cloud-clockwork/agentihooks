"""Periodic checkpoint capture with bounded overhead, and an emergency drain with a budget of its own."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from scripts.swarm_v2.artifacts.base import ArtifactError
from scripts.swarm_v2.checkpoints.publisher import SUPERSEDED, Publication, Publisher

AGE = "checkpoint_age_seconds"
INTERRUPTED = "interrupted"
OVER_BUDGET = "drain_budget_exceeded"
Capture = Callable[[int], tuple[dict, dict[str, bytes]]]


@dataclass(frozen=True)
class Budget:
    interval: float = 300.0
    overhead: float = 0.05
    drain: float = 30.0
    drain_attempts: int = 3


class Schedule:
    """A captured checkpoint is republished under its own identity until it lands or its generation is superseded."""

    def __init__(
        self,
        publisher: Publisher,
        capture: Capture,
        budget: Budget = Budget(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.publisher, self.capture, self.budget, self.clock = publisher, capture, budget, clock
        self.due = clock()
        self.next = publisher.sequence() + 1
        self.pending: tuple[dict, dict[str, bytes]] | None = None
        self.published_at: float | None = None
        self.stopped = False

    def metrics(self) -> dict:
        age = None if self.published_at is None else self.clock() - self.published_at
        return {AGE: age, **self.publisher.metrics()}

    def tick(self) -> Publication | None:
        if self.stopped or self.clock() < self.due:
            return None
        started = self.clock()
        result = self._attempt()
        self.due = started + max(self.budget.interval, (self.clock() - started) / self.budget.overhead)
        return result

    def drain(self) -> Publication | None:
        if self.stopped:
            return None
        deadline = self.clock() + self.budget.drain
        for _ in range(self.budget.drain_attempts):
            result = self._attempt(deadline)
            if result is not None:
                return result
        return Publication(False, INTERRUPTED)

    def _attempt(self, deadline: float | None = None) -> Publication | None:
        if self.pending is None:
            self.pending = self.capture(self.next)
            self.next += 1
        if deadline is not None and self.clock() > deadline:
            self.pending = None
            return self.publisher.refuse(OVER_BUDGET)
        try:
            result = self.publisher.publish(*self.pending)
        except (OSError, ArtifactError):
            self.publisher.failures[INTERRUPTED] += 1
            return None
        self.pending = None
        if result.promoted:
            self.published_at = self.clock()
        self.stopped = result.reason == SUPERSEDED
        return result
