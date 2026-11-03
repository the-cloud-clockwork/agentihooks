"""Registered tick steps that run a hand written call of the tick pass against the pass state."""

from collections.abc import Callable, Iterable
from functools import partial
from importlib import import_module
from typing import Any, cast

from scripts.tick.engine import StepRecord
from scripts.tick.step import Context, Domain, Lane, Result, Write
from scripts.tick.steps import budgets

PASS = Domain("pass")
AGENTS = Domain("agents")
TASKS = Domain("tasks")
SWARM = Domain("swarm")


class CallStep:
    """A step running one call of the tick module with the pass state and writing the actions it returned."""

    lane = Lane("tick")

    def __init__(
        self,
        name: str,
        group: str,
        call: Callable[[Any, Callable[[str], Any]], Any],
        needs: frozenset[str] = frozenset(),
        reads: frozenset[Domain] = frozenset(),
        writes: frozenset[Domain] = frozenset(),
        lane: Lane = lane,
    ) -> None:
        """Name the step in tick group ``group`` and its ``lane``, keep its call, its needs and the domains it reads
        and writes."""
        self.lane = lane
        self.name = name
        self.needs = needs
        self.group = group
        self.call = call
        self.budget_s, self.interval_s = budgets.timing_of(name)
        self.reads = reads
        self.writes = writes | {actions_of(name)}

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


class Left(KeyError):
    """A pass value no step stored, because its writer was held or left running past its budget."""


class PassBox(dict[str, Any]):
    """The pass box: reading a value no step stored raises ``Left`` instead of a plain ``KeyError``."""

    def __missing__(self, key: str) -> Any:
        """Raise ``Left`` naming the value no step stored."""
        raise Left(key)
