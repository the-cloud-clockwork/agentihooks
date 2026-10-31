from dataclasses import dataclass
from importlib import import_module
from typing import Any

import pytest


@dataclass(frozen=True)
class SampleStep:
    name: str
    needs: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    lane: str = "local"
    group: str = "observe"
    order: int = 0
    budget_s: float = 1.0
    interval_s: float = 0.0

    def run(self, ctx: object) -> object:
        return ctx


def registry() -> Any:
    return import_module("scripts.tick.registry")


def test_empty_registry() -> None:
    subject = registry().Registry()
    assert subject.validate() is None
    assert subject.waves() == ()


def test_waves_preserve_order_and_dependency_barriers() -> None:
    subject = registry().Registry()
    first = SampleStep("first", order=2)
    second = SampleStep("second", order=1)
    third = SampleStep("third", frozenset({"first"}))
    fourth = SampleStep("fourth", frozenset({"second", "third"}))
    for step in (first, second, third, fourth):
        subject.register(step)
    assert subject.waves() == ((second, first), (third,), (fourth,))
    assert subject.waves() == ((second, first), (third,), (fourth,))


def test_equal_orders_keep_registration_order() -> None:
    subject = registry().Registry()
    first, second = SampleStep("z"), SampleStep("a")
    subject.register(first)
    subject.register(second)
    assert subject.waves() == ((first, second),)


def test_duplicate_names_refused() -> None:
    subject = registry().Registry()
    subject.register(SampleStep("same"))
    subject.register(SampleStep("same", lane="remote"))
    with pytest.raises(ValueError) as error:
        subject.validate()
    assert str(error.value) == "Duplicate step name: same"


@pytest.mark.parametrize(
    "steps,message",
    [
        ((SampleStep("a", frozenset({"a"})),), "Dependency cycle: a"),
        (
            (SampleStep("a", frozenset({"b"})), SampleStep("b", frozenset({"a"}))),
            "Dependency cycle: a, b",
        ),
        ((SampleStep("a", frozenset({"missing"})),), "Unknown dependency for a: missing"),
    ],
)
def test_invalid_dependencies_refused(steps: tuple[SampleStep, ...], message: str) -> None:
    subject = registry().Registry()
    for step in steps:
        subject.register(step)
    with pytest.raises(ValueError) as error:
        subject.waves()
    assert str(error.value) == message


def test_same_lane_shared_writers_require_needs_path() -> None:
    subject = registry().Registry()
    subject.register(SampleStep("a", writes=frozenset({"agents", "tasks"})))
    subject.register(SampleStep("b", writes=frozenset({"tasks", "config"})))
    with pytest.raises(ValueError) as error:
        subject.validate()
    assert str(error.value) == "Unordered writers in lane local for tasks: a, b"


def test_validation_continues_after_unrelated_lane_pair() -> None:
    subject = registry().Registry()
    subject.register(SampleStep("remote", lane="remote"))
    subject.register(SampleStep("a", writes=frozenset({"agents", "tasks"})))
    subject.register(SampleStep("b", writes=frozenset({"agents", "tasks"})))
    with pytest.raises(ValueError) as error:
        subject.validate()
    assert str(error.value) == "Unordered writers in lane local for agents, tasks: a, b"


def test_multiple_unknown_dependencies_reported_in_order() -> None:
    subject = registry().Registry()
    subject.register(SampleStep("a", frozenset({"z", "b"})))
    with pytest.raises(ValueError) as error:
        subject.validate()
    assert str(error.value) == "Unknown dependency for a: b, z"


@pytest.mark.parametrize("budget", [0.0, -1.0, float("nan"), float("inf"), float("-inf")])
def test_step_budget_must_be_finite_and_positive(budget: float) -> None:
    subject = registry().Registry()
    subject.register(SampleStep("a", budget_s=budget))
    with pytest.raises(ValueError) as error:
        subject.validate()
    assert str(error.value) == "Invalid budget for a: expected finite positive seconds"


@pytest.mark.parametrize("interval", [-1.0, float("nan"), float("inf"), float("-inf")])
def test_step_interval_must_be_finite_and_nonnegative(interval: float) -> None:
    subject = registry().Registry()
    subject.register(SampleStep("a", interval_s=interval))
    with pytest.raises(ValueError) as error:
        subject.waves()
    assert str(error.value) == "Invalid interval for a: expected finite nonnegative seconds"


@pytest.mark.parametrize("reverse", [False, True])
def test_transitive_needs_orders_shared_writers(reverse: bool) -> None:
    subject = registry().Registry()
    steps = (
        SampleStep("a", writes=frozenset({"agents"})),
        SampleStep("bridge", frozenset({"a"})),
        SampleStep("b", frozenset({"bridge"}), frozenset({"agents"})),
    )
    for step in reversed(steps) if reverse else steps:
        subject.register(step)
    assert subject.validate() is None
    assert subject.waves() == tuple((step,) for step in steps)


def test_other_lanes_and_disjoint_domains_are_independent() -> None:
    subject = registry().Registry()
    steps = (
        SampleStep("a", writes=frozenset({"agents"})),
        SampleStep("b", writes=frozenset({"agents"}), lane="remote"),
        SampleStep("c", writes=frozenset({"tasks"})),
    )
    for step in steps:
        subject.register(step)
    assert subject.waves() == (steps,)


def test_module_registry_api() -> None:
    module = registry()
    original = module.registry
    try:
        module.registry = module.Registry()
        step = SampleStep("only")
        module.register(step)
        assert module.validate() is None
        assert module.waves() == ((step,),)
    finally:
        module.registry = original
