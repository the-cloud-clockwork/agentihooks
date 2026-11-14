"""The sites of the flat pass graph: the tick's site table, its checks between sites and the graph runner."""

from collections.abc import Callable, Mapping
from contextvars import Token
from dataclasses import dataclass
from functools import partial
from importlib import import_module
from typing import Any

from scripts.swarm import reaper
from scripts.tick import engine, hook_reads, steps
from scripts.tick import registry as step_registry
from scripts.tick.adapters.processes import PassProcesses
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Step, Write
from scripts.tick.steps import masters as master_steps
from scripts.tick.steps import observe as observe_steps
from scripts.tick.steps import place
from scripts.tick.steps import tasks as task_steps

tick: Any = import_module("scripts.swarm.tick")

TICK_SITES = (
    "start",
    observe_steps.PASS_SETUP,
    "observe",
    "agents",
    master_steps.MASTERS,
    task_steps.DRAINED,
    "tasks",
    task_steps.REOPENS,
    task_steps.TASK_PASSES,
    "place",
    place.NOTICES,
    place.ACKS,
    master_steps.SEATS,
    master_steps.DISPATCH,
    place.PLACEMENT,
    master_steps.WATCH,
    task_steps.CLOSE,
)
ASLEEP_SITES = frozenset({master_steps.SEATS, place.PLACEMENT})
TICK_KEYS = frozenset({"config", "doc", "rows"})
START = "start"
BOX = "box"
SLEEPING = "sleeping"
FINISHED = "finished"
CONFIG = "config"
TICK_HOOK_READS = {"agents": TICK_KEYS, "tasks": frozenset({CONFIG})}
TICK_HOOK_KEYS: dict[str, tuple[frozenset[Domain], frozenset[Domain]]] = {
    START: (frozenset(), frozenset()),
    "agents": (steps.pass_domains(*TICK_KEYS), frozenset()),
    "tasks": (steps.pass_domains(CONFIG), frozenset()),
}
TICK_SHUT_KEYS: dict[str, frozenset[Domain]] = {
    site: steps.pass_domains(FINISHED, *((SLEEPING,) if site in ASLEEP_SITES else ()))
    for site in TICK_SITES
    if site != START
}


class _GraphView:
    """The view of one pass graph: its box, the sleeping answer, then each box value over the pass state."""

    def __init__(self, state: Mapping[str, object], box: dict[str, Any]) -> None:
        """Keep the pass state and the box the graph's steps write."""
        self._state = state
        self._box = box

    def read(self, domain: Domain, key: str) -> object | None:
        """Return the box, the sleeping answer, the box value or else the pass state under ``key`` for the pass
        domain, nothing for any other domain."""
        if domain != steps.PASS:
            return None
        if key == BOX:
            return self._box
        if key == SLEEPING:
            return task_steps.sleeping_of(self._box)
        return self._box[key] if key in self._box else self._state.get(key)


@dataclass
class TickSites:
    """The tick's sites of a pass graph: the checks the hand written tick ran between its groups."""

    state: dict[str, Any]
    box: dict[str, Any]
    apply: dict[Domain, Callable[[Write], list[str] | None]]
    clock: Callable[[], int]
    scanning: bool = False
    left: bool = False
    left_from: str = START
    scan: Token[PassProcesses | None] | None = None

    def hooks(self) -> dict[str, Callable[[], bool]]:
        """Return the checks run before the start, agents and tasks sites, each reading and writing the keys
        ``TICK_HOOK_KEYS`` declares for its site and guarded on the box keys it reads."""
        found = {START: self._start, "agents": self._listed, "tasks": self._configured}
        return hook_reads.guarded({site: found[site] for site in TICK_HOOK_KEYS}, TICK_HOOK_READS)

    def shut(self, step: Step) -> bool:
        """Return whether ``step`` stays shut: every step once a step held an exception; a tick site past the start
        once the stopped branch finished the pass, from the site whose hook found a value the tick reads left, and a
        placement site while the swarm sleeps."""
        if observe_steps.RAISED in self.box:
            return True
        if step.group not in TICK_SITES or step.group == START:
            return False
        asleep = step.group in ASLEEP_SITES and task_steps.sleeping_of(self.box)
        left = self.left and TICK_SITES.index(step.group) >= TICK_SITES.index(self.left_from)
        return FINISHED in self.box or left or asleep

    def close(self) -> None:
        """End the process table scan the start check began, once."""
        if self.scan is not None:
            reaper.PASS.reset(self.scan)
            self.scan = None

    def _start(self) -> bool:
        """Load the master move modules, start the scan when asked, read the tick clock and add its appliers."""
        for name in tick.MASTER_MOVE_MODULES:
            import_module(name)
        if self.scanning:
            self.scan = reaper.PASS.set(reaper.PassProcesses(lambda: reaper.processes()))
        self.state["now_ms"] = self.clock()
        self.apply.update(task_steps.appliers(self.state))
        return True

    def _listed(self) -> bool:
        """Leave the tick's actions out when observation left a value the tick reads."""
        return self._leave(not TICK_KEYS <= self.box.keys(), "agents")

    def _configured(self) -> bool:
        """Leave the tick's actions out when the drained resume left the swarm config."""
        return self._leave(CONFIG not in self.box, "tasks")

    def _leave(self, missing: bool, site: str) -> bool:
        """Mark the tick left from ``site`` when ``missing`` and no earlier site left it, clear the mark ``site`` set
        once its value is back; answer whether the site runs."""
        if missing and not self.left:
            self.left, self.left_from = True, site
        elif not missing and self.left_from == site:
            self.left = False
        return not missing


def run_sites(
    sites: tuple[str, ...],
    state: Mapping[str, object],
    box: dict[str, Any],
    gate: engine.SiteGate,
    apply: engine.Appliers,
    hooks: Mapping[str, step_registry.HookKeys] | None = None,
    shuts: Mapping[str, frozenset[Domain]] | None = None,
) -> tuple[engine.StepRecord, ...]:
    """Run every registered step of ``sites`` as one pass graph, each step after the steps of earlier sites whose
    declared values it uses, the site ``hooks`` and the ``shuts`` values included.

    A step runs as ``gate`` says; slow lane steps that end after the graph returns print their actions.
    """
    order = {site: index for index, site in enumerate(sites)}
    found = sorted((step for step in step_registry.steps() if step.group in order), key=lambda step: order[step.group])
    ctx = Context(_GraphView(state, box), Deadline.after(tick.GROUP_BUDGET_S))
    edged = step_registry.site_edges(found, sites, hooks, shuts)
    records = engine.run_graph(edged, ctx, apply, partial(tick._print_actions, box), gate)
    steps.note_left(box, records)
    return records
