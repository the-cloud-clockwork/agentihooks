"""Registered tick steps that run a hand written call of the tick pass against the pass state."""

from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping
from functools import partial
from importlib import import_module
from typing import Any, TypedDict, Unpack, cast

from scripts.tick import registry
from scripts.tick.engine import StepRecord
from scripts.tick.step import Context, Domain, Lane, Result, Write
from scripts.tick.steps import budgets

PASS = Domain("pass")
AGENTS = Domain("agents")
TASKS = Domain("tasks")
SWARM = Domain("swarm")
DEADLINE = "deadline"
BOX = "box"
RAISED = "raised"
PASS_PREFIX = "pass:"
HOOK_PREFIX = "hook:"
BOX_KEYS = frozenset(
    {
        "agents",
        "config",
        "count",
        "doc",
        "finished",
        "found",
        "judged",
        "live",
        "phase changed",
        "rows",
        "sleeping",
        "stop",
        "view",
    }
)


class Declared(TypedDict, total=False):
    """What a step declares beyond its graph: the ports it may write through, its feature map id and whether it is
    kept, run even after its lane expired or a step it needs was dropped."""

    effects: frozenset[str]
    feature: str
    kept: bool


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
        self.kept = declared.get("kept", False)
        self.name = name
        self.needs = needs
        self.group = group
        self.call = call
        self.interval_s = budgets.timing_of(name)[1]
        self.floor_s = budgets.floor_of(name)
        self.reads = reads
        self.writes = writes | {actions_of(name)}

    @property
    def budget_s(self) -> float:
        """Return the step's budget this pass: its live budget, never under its floor."""
        return budgets.floored(self.name, self.floor_s)

    def run(self, ctx: Context) -> Result:
        """Run the call with the tick module and a reader of the pass state, writing its actions."""
        reader = partial(declared_read, ctx, self.name, declared_keys(self.reads | self.writes))
        found = self.call(import_module("scripts.swarm.tick"), reader)
        return Result((Write(actions_of(self.name), self.name, list(found or [])),))


def declared_read(ctx: Context, step: str, allowed: frozenset[str], key: str) -> Any:
    """Return the step deadline or tools, else the pass value under key once step ``step`` declares it; the pass
    box comes back as a view refusing every box value the step does not declare."""
    if key == "tools":
        return ctx.tools
    if key == DEADLINE:
        return ctx.deadline
    found = ctx.view.read(PASS, key)
    if key == BOX or isinstance(found, PassBox):
        return DeclaredBox(cast(dict[str, Any], found), step, allowed)
    box = ctx.view.read(PASS, BOX)
    if key not in allowed and (key in BOX_KEYS or (isinstance(box, Mapping) and key in box)):
        refuse(step, key)
    return found


def actions_of(name: str) -> Domain:
    """Return the domain step ``name`` writes its actions to."""
    return Domain(f"actions:{name}")


def pass_domain(key: str) -> Domain:
    """Return the domain of the pass box value stored under ``key``."""
    return Domain(f"pass:{key}")


def pass_domains(*keys: str) -> frozenset[Domain]:
    """Return the domains of the pass box values stored under ``keys``."""
    return frozenset(map(pass_domain, keys))


def hook_domain(key: str) -> Domain:
    """Return the domain of the pass box value a site hook stores under ``key``."""
    return Domain(f"{HOOK_PREFIX}{key}")


def hook_domains(*keys: str) -> frozenset[Domain]:
    """Return the domains of the pass box values site hooks store under ``keys``."""
    return frozenset(map(hook_domain, keys))


def declared_keys(domains: Iterable[Domain]) -> frozenset[str]:
    """Return the pass box keys ``domains`` name, through step and site hook domains alike, with the held
    exception every step may read."""
    keys = {domain.split(":", 1)[1] for domain in domains if domain.startswith((PASS_PREFIX, HOOK_PREFIX))}
    return frozenset(keys | {RAISED})


class UndeclaredRead(Exception):
    """A step read a pass box value its reads and writes do not declare."""


def refuse(step: str, key: str) -> None:
    """Raise ``UndeclaredRead`` naming step ``step`` and the pass value ``key`` it does not declare."""
    raise UndeclaredRead(f"step {step} reads pass value {key} it does not declare")


class DeclaredBox(MutableMapping[str, Any]):
    """The pass box as one step sees it: reading a box value the step does not declare raises ``UndeclaredRead``,
    writes go to the box itself."""

    def __init__(self, box: dict[str, Any], step: str, allowed: frozenset[str]) -> None:
        """Keep the pass ``box``, the name of the step reading it and the keys it declares."""
        self.box = box
        self.step = step
        self.allowed = allowed

    def _check(self, key: str) -> None:
        """Refuse ``key`` when it names a box value, known or stored, the step does not declare."""
        if key not in self.allowed and (key in BOX_KEYS or key in self.box):
            refuse(self.step, key)

    def __getitem__(self, key: str) -> Any:
        """Return the box value under ``key`` once the step declares it, raising ``Left`` when no step stored it."""
        self._check(key)
        if key not in self.box:
            raise Left(key)
        return self.box[key]

    def __contains__(self, key: object) -> bool:
        """Return whether the box holds ``key`` once the step declares it."""
        self._check(str(key))
        return key in self.box

    def __setitem__(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key`` in the box."""
        self.box[key] = value

    def __delitem__(self, key: str) -> None:
        """Remove ``key`` from the box."""
        del self.box[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate the box keys the step declares."""
        return iter([key for key in self.box if key in self.allowed])

    def __len__(self) -> int:
        """Return the number of box keys the step declares."""
        return len(list(iter(self)))


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
    """Add to a pass ``box`` the name of each recorded step the pass stopped waiting on (abandoned at its
    budget, skipped or cut at its lane end) or held."""
    if isinstance(box, PassBox):
        box.left.extend(record.name for record in records if record.lost or record.held)


MODULES = (
    "scripts.tick.steps.agents",
    "scripts.tick.steps.claims",
    "scripts.tick.steps.common",
    "scripts.tick.steps.host",
    "scripts.tick.steps.masters",
    "scripts.tick.steps.observe",
    "scripts.tick.steps.place",
    "scripts.tick.steps.report",
    "scripts.tick.steps.tasks",
    "scripts.tick.steps.territory",
    "scripts.tick.steps.workflows",
)

for _module in MODULES:
    registry.require(_module)
