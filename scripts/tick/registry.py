from itertools import combinations
from math import isfinite

from scripts.tick.step import Step

__all__ = ["Registry", "register", "registry", "validate", "waves"]


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
    pending = sorted(steps, key=lambda step: step.order)
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
        if first.lane != second.lane:
            continue
        shared = first.writes & second.writes
        if shared and first.name not in ancestors[second.name] and second.name not in ancestors[first.name]:
            domains = ", ".join(sorted(shared))
            raise ValueError(f"Unordered writers in lane {first.lane} for {domains}: {first.name}, {second.name}")


class Registry:
    def __init__(self) -> None:
        self._steps: list[Step] = []

    def register(self, step: Step) -> None:
        self._steps.append(step)

    def validate(self) -> None:
        _validate_writers(_dependency_waves(self._steps))

    def waves(self) -> tuple[tuple[Step, ...], ...]:
        ordered = _dependency_waves(self._steps)
        _validate_writers(ordered)
        return ordered


registry = Registry()


def register(step: Step) -> None:
    registry.register(step)


def validate() -> None:
    registry.validate()


def waves() -> tuple[tuple[Step, ...], ...]:
    return registry.waves()
