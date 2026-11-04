"""Registered tick steps that run a hand written call of the tick pass against the pass state."""

from collections.abc import Callable, Iterable, Mapping
from functools import partial
from importlib import import_module
from typing import Any, TypedDict, Unpack, cast

from scripts.tick.engine import StepRecord
from scripts.tick.step import Context, Domain, Lane, Result, Write
from scripts.tick.steps import budgets

PASS = Domain("pass")
AGENTS = Domain("agents")
TASKS = Domain("tasks")
SWARM = Domain("swarm")


class Declared(TypedDict, total=False):
    """What a step declares beyond its graph: the ports it may write through and its feature map id."""

    effects: frozenset[str]
    feature: str


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
        **declared: Unpack[Declared],
    ) -> None:
        """Name the step in tick group ``group`` and its ``lane``, keep its call, its needs, the domains it reads
        and writes, the ports it may write through and its feature map id."""
        self.lane = lane
        self.effects = declared.get("effects", frozenset())
        self.feature = declared.get("feature", "")
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


def pass_domain(key: str) -> Domain:
    """Return the domain of the pass box value stored under ``key``."""
    return Domain(f"pass:{key}")


def pass_domains(*keys: str) -> frozenset[Domain]:
    """Return the domains of the pass box values stored under ``keys``."""
    return frozenset(map(pass_domain, keys))


def actions(records: Iterable[StepRecord]) -> list[str]:
    """Return the actions the recorded steps wrote to their own domains, then each write they dropped, in step order."""
    return [action for record in records for action in _step_actions(record)]


def _step_actions(record: StepRecord) -> list[str]:
    """Return one step's actions followed by a line naming each write the engine dropped and why."""
    if not record.result:
        return []
    found = [
        action
        for write in record.result.writes
        if write.domain == actions_of(record.name)
        for action in cast(list[str], write.value)
    ]
    return (
        found
        + list(record.applied)
        + [
            f"dropped write {write.domain} {write.key} of step {record.name}: {why}; the next pass recomputes it"
            for write, why in zip(record.dropped, record.conflicts, strict=True)
        ]
    )


class Left(KeyError):
    """A pass value no step stored, because its writer was held or left running past its budget."""


class PassBox(dict[str, Any]):
    """The pass box: reading a value no step stored raises ``Left`` instead of a plain ``KeyError``.

    ``left`` names each step the pass left running, dropped or held, shared by every box handed the same list.
    """

    def __init__(self, values: Mapping[str, Any] | None = None, *, left: list[str] | None = None) -> None:
        """Start a box holding ``values`` and keeping the names of the steps its pass left in ``left``, a new list when
        not given."""
        super().__init__(values or {})
        self.left: list[str] = [] if left is None else left

    def __missing__(self, key: str) -> Any:
        """Raise ``Left`` naming the value no step stored."""
        raise Left(key)


def note_left(box: object, records: Iterable[StepRecord]) -> None:
    """Add to a pass ``box`` the name of each recorded step left running past its budget, dropped or held."""
    if isinstance(box, PassBox):
        box.left.extend(record.name for record in records if record.abandoned or record.held)
