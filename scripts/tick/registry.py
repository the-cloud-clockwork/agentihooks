from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import copy
from dataclasses import dataclass
from importlib import import_module
from itertools import combinations
from math import isfinite

from scripts.tick.pool import RESERVES
from scripts.tick.step import Domain, Step

__all__ = [
    "Catalogue",
    "Registry",
    "latest_start",
    "ordered",
    "register",
    "registry",
    "require",
    "site_edges",
    "steps",
    "validate",
    "waves",
]


@dataclass(frozen=True)
class Catalogue:
    """The feature map ids, the scenario names listing each step by name and the steps allowed to lack either."""

    features: frozenset[str]
    scenarios: Mapping[str, frozenset[str]]
    unmapped: frozenset[str] = frozenset()


def _catalogue_gaps(step: Step, catalogue: Catalogue) -> list[str]:
    """Each way ``step`` lacks a feature map entry or a scenario in ``catalogue``."""
    reasons = [f"feature {step.feature!r} is not a feature map id"] if step.feature not in catalogue.features else []
    return reasons + ([] if catalogue.scenarios.get(step.name) else ["no scenario lists it"])


def _catalogue_errors(steps: list[Step], catalogue: Catalogue) -> list[str]:
    """Refuse each step lacking a feature map entry or a scenario unless unmapped, and each stale unmapped entry."""
    errors: list[str] = []
    for step in steps:
        reasons = _catalogue_gaps(step, catalogue)
        if reasons and step.name not in catalogue.unmapped:
            errors.append(f"Step {step.name}: {'; '.join(reasons)}")
        elif not reasons and step.name in catalogue.unmapped:
            errors.append(f"Step {step.name} is mapped; drop it from the unmapped steps")
    stale = catalogue.unmapped - {step.name for step in steps}
    return errors + [f"Unmapped step {name} is not registered; drop it" for name in sorted(stale)]


PASS_BOX = "pass:"
HOOK_BOX = "hook:"
HookKeys = tuple[frozenset[Domain], frozenset[Domain]]
PASS_BOX_INTERVAL_S = 60.0
SLOW_LANE = "slow"


def _validate_timing(steps: list[Step]) -> None:
    for step in steps:
        if not isfinite(step.budget_s) or step.budget_s <= 0:
            raise ValueError(f"Invalid budget for {step.name}: expected finite positive seconds")
        if not isfinite(step.interval_s) or step.interval_s < 0:
            raise ValueError(f"Invalid interval for {step.name}: expected finite nonnegative seconds")
        boxes = sorted(domain for domain in step.writes if domain.startswith(PASS_BOX))
        if step.interval_s > PASS_BOX_INTERVAL_S and boxes:
            raise ValueError(f"Interval of {step.name} above {PASS_BOX_INTERVAL_S:g}s writes {', '.join(boxes)}")


def _dependency_waves(steps: list[Step]) -> tuple[tuple[Step, ...], ...]:
    _validate_timing(steps)
    names: set[str] = set()
    for step in steps:
        if step.name in names:
            raise ValueError(f"Duplicate step name: {step.name}")
        names.add(step.name)
    for step in steps:
        missing = step.needs - names
        if missing:
            raise ValueError(f"Unknown dependency for {step.name}: {', '.join(sorted(missing))}")
    pending = list(steps)
    completed: set[str] = set()
    result: list[tuple[Step, ...]] = []
    for _ in steps:
        if not pending:
            break
        ready = tuple(step for step in pending if step.needs <= completed)
        if not ready:
            raise ValueError(f"Dependency cycle: {', '.join(sorted(step.name for step in pending))}")
        result.append(ready)
        completed.update(step.name for step in ready)
        pending = [step for step in pending if step.name not in completed]
    return tuple(result)


def _validate_writers(ordered: tuple[tuple[Step, ...], ...]) -> None:
    ancestors: dict[str, set[str]] = {}
    steps = [step for wave in ordered for step in wave]
    for step in steps:
        ancestors[step.name] = set(step.needs)
        for dependency in step.needs:
            ancestors[step.name].update(ancestors[dependency])
    for first, second in combinations(steps, 2):
        if first.name in ancestors[second.name] or second.name in ancestors[first.name]:
            continue
        where = f"lane {first.lane}" if first.lane == second.lane else f"lanes {first.lane} and {second.lane}"
        shared = first.writes & second.writes
        if shared:
            domains = ", ".join(sorted(shared))
            raise ValueError(f"Unordered writers in {where} for {domains}: {first.name}, {second.name}")
        crossed = (first.writes & second.reads) | (first.reads & second.writes)
        if crossed:
            domains = ", ".join(sorted(crossed))
            raise ValueError(f"Unordered reader and writer in {where} for {domains}: {first.name}, {second.name}")


class Registry:
    def __init__(self) -> None:
        self._steps: list[Step] = []
        self._providers: list[str] = []

    def register(self, step: Step) -> None:
        self._steps.append(step)

    def require(self, module: str) -> None:
        """Keep ``module``, whose steps a registered step needs, to load before the registry is next read."""
        self._providers.append(module)

    def _load_providers(self) -> None:
        for module in self._providers:
            import_module(module)

    def validate(self, catalogue: Catalogue | None = None) -> None:
        """Validate the needs, reads and writes of the steps, and with a ``catalogue`` their feature and scenario."""
        self._load_providers()
        _validate_writers(_dependency_waves(self._steps))
        errors = [] if catalogue is None else _catalogue_errors(self._steps, catalogue)
        if errors:
            raise ValueError("\n".join(errors))

    def waves(self) -> tuple[tuple[Step, ...], ...]:
        self._load_providers()
        ordered = _dependency_waves(self._steps)
        _validate_writers(ordered)
        return ordered

    def steps(self) -> tuple[Step, ...]:
        """Return the registered steps in registration order once their needs, reads and writes validate."""
        self.validate()
        return tuple(self._steps)


registry = Registry()
registry.require("scripts.tick.steps")


def register(step: Step) -> None:
    registry.register(step)


def require(module: str) -> None:
    """Load ``module``, whose steps a registered step needs, before the registry is next read."""
    registry.require(module)


def validate(catalogue: Catalogue | None = None) -> None:
    """Validate the registered steps, against ``catalogue`` when given."""
    registry.validate(catalogue)


def waves() -> tuple[tuple[Step, ...], ...]:
    return registry.waves()


def steps() -> tuple[Step, ...]:
    """Return the registered steps in registration order once they validate."""
    return registry.steps()


def ordered(steps: Iterable[Step]) -> tuple[Step, ...]:
    """Return ``steps`` with each after the steps among them it needs, otherwise in the order given."""
    pending = list(steps)
    names = {step.name for step in pending}
    placed: list[Step] = []
    done: set[str] = set()
    for _ in range(len(pending)):
        ready = next((step for step in pending if step.needs & names <= done), None)
        if ready is None:
            raise ValueError(f"Dependency cycle: {', '.join(sorted(step.name for step in pending))}")
        placed.append(ready)
        done.add(ready.name)
        pending.remove(ready)
    return tuple(placed)


def latest_start(name: str, steps: list[Step]) -> float:
    """Return the latest start of step ``name`` in seconds when every step it needs ends by its floor, or by its
    budget when it declares no floor."""
    floors = {step.name: getattr(step, "floor_s", step.budget_s) for step in steps}
    starts: dict[str, float] = {}
    for wave in _dependency_waves(steps):
        for step in wave:
            starts[step.name] = max((starts[need] + floors[need] for need in step.needs), default=0.0)
    return starts[name]


def _needing(step: Step, needs: frozenset[str]) -> Step:
    """Return a copy of ``step`` that needs exactly ``needs``."""
    edged = copy(step)
    object.__setattr__(edged, "needs", needs)
    return edged


def _boxed(domains: Iterable[Domain]) -> frozenset[Domain]:
    """Return ``domains`` with each site hook value named as the pass value of the same name, one box key."""
    return frozenset(
        Domain(PASS_BOX + domain.removeprefix(HOOK_BOX)) if domain.startswith(HOOK_BOX) else domain
        for domain in domains
    )


def _backward(steps: tuple[Step, ...], sites: Sequence[str]) -> tuple[Step, ...]:
    """Return ``steps`` with each step of a site in ``sites`` no longer needing a step of a later site."""
    order = {site: index for index, site in enumerate(sites)}
    at = {step.name: order[step.group] for step in steps if step.group in order}
    found: list[Step] = []
    for step in steps:
        later = frozenset(need for need in step.needs if step.name in at and at.get(need, -1) > at[step.name])
        found.append(_needing(step, step.needs - later) if later else step)
    return tuple(found)


def site_edges(
    steps: Iterable[Step],
    sites: Sequence[str],
    hooks: Mapping[str, HookKeys] | None = None,
    shuts: Mapping[str, frozenset[Domain]] | None = None,
) -> tuple[Step, ...]:
    """Return ``steps`` in order, each step of a site in ``sites`` also needing each step of an earlier site that
    writes a value it reads or writes, reads a value it writes, or writes a value the hook of a site between them,
    its own included, reads or writes; steps of groups outside ``sites`` keep their own needs.

    A step of a site needs no step of a later site. ``hooks`` holds the values each site hook reads and writes and
    ``shuts`` the values the shut check reads for the steps of each site; a hook value and a pass value of the same
    name are one box value. A slow lane or reserved step, and every step needing one, is no edge of a later site.
    """
    found = _backward(tuple(steps), sites)
    order = {site: index for index, site in enumerate(sites)}
    late = _late(found)
    early = tuple(step for step in found if step.group in order and step.name not in late)
    hooked = {site: _boxed(reads | writes) for site, (reads, writes) in (hooks or {}).items() if site in order}
    edged: list[Step] = []
    for step in found:
        if step.group not in order:
            edged.append(step)
            continue
        uses = _Uses.of(step, (shuts or {}).get(step.group, frozenset()))
        held = _hook_values(hooked, order, order[step.group])
        needs = frozenset(
            other.name
            for other in early
            if order[other.group] < order[step.group] and uses.after(other, held(order[other.group]))
        )
        edged.append(_needing(step, step.needs | needs))
    return tuple(edged)


@dataclass(frozen=True)
class _Uses:
    """The values one sited step reads, its shut check included, and writes."""

    reads: frozenset[Domain]
    writes: frozenset[Domain]

    @classmethod
    def of(cls, step: Step, shut: frozenset[Domain]) -> "_Uses":
        """Return the values ``step`` reads, with ``shut``, and writes."""
        return cls(step.reads | shut, step.writes)

    def after(self, other: Step, hooked: frozenset[Domain]) -> bool:
        """Return whether the step runs after ``other``: ``other`` writes a value it reads or writes, or the box key
        of a ``hooked`` value, or reads a value it writes."""
        written = other.writes & (self.reads | self.writes) or _boxed(other.writes) & hooked
        return bool(written or other.reads & self.writes)


def _hook_values(
    hooked: Mapping[str, frozenset[Domain]], order: Mapping[str, int], at: int
) -> Callable[[int], frozenset[Domain]]:
    """Return the values the hooks of the sites after a given site and up to site ``at`` read or write."""

    def between(start: int) -> frozenset[Domain]:
        return frozenset[Domain]().union(*(keys for site, keys in hooked.items() if start < order[site] <= at))

    return between


def _late(steps: tuple[Step, ...]) -> frozenset[str]:
    """Return the slow lane and reserved steps of ``steps`` and every step needing one of them, directly or through
    another. A reserved step runs on into the reserve it holds at the end of its lane."""
    late: set[str] = set()
    for step in ordered(steps):
        if step.lane == SLOW_LANE or step.name in RESERVES or step.needs & late:
            late.add(step.name)
    return frozenset(late)
