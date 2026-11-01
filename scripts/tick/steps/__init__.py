"""Registered tick steps that run a hand written call of the tick pass against the pass state."""

from collections.abc import Callable, Iterable
from functools import partial
from importlib import import_module
from typing import Any, cast

from scripts.tick.engine import StepRecord
from scripts.tick.step import Context, Domain, Lane, Result, Write

PASS = Domain("pass")


class CallStep:
    """A step running one call of the tick module with the pass state and writing the actions it returned."""

    lane = Lane("tick")
    budget_s = 600.0
    interval_s = 0.0

    def __init__(
        self,
        name: str,
        group: str,
        order: int,
        call: Callable[[Any, Callable[[str], Any]], Any],
        needs: frozenset[str] = frozenset(),
    ) -> None:
        """Name the step, place it at ``order`` in tick group ``group``, keep the call it runs and the steps it needs."""
        self.name = name
        self.needs = needs
        self.group = group
        self.order = order
        self.call = call
        self.writes = frozenset({actions_of(name)})

    def run(self, ctx: Context) -> Result:
        """Run the call with the tick module and a reader of the pass state, writing its actions."""
        found = self.call(import_module("scripts.swarm.tick"), partial(ctx.view.read, PASS))
        return Result((Write(actions_of(self.name), self.name, list(found or [])),))


def actions_of(name: str) -> Domain:
    """Return the domain step ``name`` writes its actions to."""
    return Domain(f"actions:{name}")


def actions(records: Iterable[StepRecord]) -> list[str]:
    """Return the actions the recorded steps wrote to their own domains, in step order."""
    return [
        action
        for record in records
        if record.result
        for write in record.result.writes
        if write.domain == actions_of(record.name)
        for action in cast(list[str], write.value)
    ]
