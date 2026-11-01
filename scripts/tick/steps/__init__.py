"""Registered tick steps that run a hand written call of the tick pass against the pass state."""

from collections.abc import Callable, Iterable
from functools import partial
from importlib import import_module
from typing import Any, cast

from scripts.tick.engine import StepRecord
from scripts.tick.step import Context, Domain, Lane, Result, Write

__all__ = ["ACTIONS", "BUDGET_S", "LANE", "PASS", "CallStep", "actions"]

PASS = Domain("pass")
ACTIONS = Domain("actions")
LANE = Lane("tick")
BUDGET_S = 600.0


class CallStep:
    """A step running one call of the tick module with the pass state and writing the actions it returned."""

    def __init__(self, name: str, group: str, order: int, call: Callable[[Any, Callable[[str], Any]], Any]) -> None:
        """Name the step, place it at ``order`` in tick group ``group`` and keep the call it runs."""
        self.name = name
        self.group = group
        self.order = order
        self.call = call
        self.lane = LANE
        self.budget_s = BUDGET_S
        self.interval_s = 0.0
        self.needs: frozenset[str] = frozenset()
        self.writes: frozenset[Domain] = frozenset()

    def run(self, ctx: Context) -> Result:
        """Run the call with the tick module and a reader of the pass state, writing its actions."""
        found = self.call(import_module("scripts.swarm.tick"), partial(ctx.view.read, PASS))
        return Result((Write(ACTIONS, self.name, list(found or [])),))


def actions(records: Iterable[StepRecord]) -> list[str]:
    """Return the actions the recorded steps wrote, in step order."""
    found: list[str] = []
    for record in records:
        for write in record.result.writes if record.result else ():
            if write.domain == ACTIONS:
                found += cast(list[str], write.value)
    return found
