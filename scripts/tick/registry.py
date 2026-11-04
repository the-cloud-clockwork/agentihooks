from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations
from math import isfinite

from scripts.tick.step import Step

__all__ = [
    "Catalogue",
    "Registry",
    "latest_start",
    "ordered",
    "register",
    "registry",
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


def _unmapped_reasons(step: Step, catalogue: Catalogue) -> list[str]:
    """Each way ``step`` lacks a feature map entry or a scenario in ``catalogue``."""
    reasons = [f"feature {step.feature!r} is not a feature map id"] if step.feature not in catalogue.features else []
    return reasons + ([] if catalogue.scenarios.get(step.name) else ["no scenario lists it"])


def _catalogue_errors(steps: list[Step], catalogue: Catalogue) -> list[str]:
    """Refuse each step lacking a feature map entry or a scenario unless unmapped, and each stale unmapped entry."""
    errors: list[str] = []
    for step in steps:
        reasons = _unmapped_reasons(step, catalogue)
        if reasons and step.name not in catalogue.unmapped:
            errors.append(f"Step {step.name}: {'; '.join(reasons)}")
        elif not reasons and step.name in catalogue.unmapped:
            errors.append(f"Step {step.name} is mapped; drop it from the unmapped steps")
    stale = catalogue.unmapped - {step.name for step in steps}
    return errors + [f"Unmapped step {name} is not registered; drop it" for name in sorted(stale)]


def _validate_timing(steps: list[Step]) -> None:
    for step in steps:
        if not isfinite(step.budget_s) or step.budget_s <= 0:
            raise ValueError(f"Invalid budget for {step.name}: expected finite positive seconds")
        if not isfinite(step.interval_s) or step.interval_s < 0:
            raise ValueError(f"Invalid interval for {step.name}: expected finite nonnegative seconds")


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

    def register(self, step: Step) -> None:
        self._steps.append(step)

    def validate(self, catalogue: Catalogue | None = None) -> None:
        """Validate the needs, reads and writes of the steps, and with a ``catalogue`` their feature and scenario."""
        _validate_writers(_dependency_waves(self._steps))
        errors = [] if catalogue is None else _catalogue_errors(self._steps, catalogue)
        if errors:
            raise ValueError("\n".join(errors))

    def waves(self) -> tuple[tuple[Step, ...], ...]:
        ordered = _dependency_waves(self._steps)
        _validate_writers(ordered)
        return ordered

    def steps(self) -> tuple[Step, ...]:
        """Return the registered steps in registration order once their needs, reads and writes validate."""
        self.validate()
        return tuple(self._steps)


registry = Registry()


def register(step: Step) -> None:
    registry.register(step)


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
    """Return the latest start of step ``name`` in seconds when every step it needs ends by its budget."""
    by_name = {step.name: step for step in steps}
    starts: dict[str, float] = {}
    for wave in _dependency_waves(steps):
        for step in wave:
            starts[step.name] = max((starts[need] + by_name[need].budget_s for need in step.needs), default=0.0)
    return starts[name]
