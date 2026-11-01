from dataclasses import dataclass
from importlib import import_module
from typing import Any

import pytest


@dataclass(frozen=True)
class SampleStep:
    name: str
    needs: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    reads: frozenset[str] = frozenset()
    lane: str = "local"
    group: str = "observe"
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


def test_waves_keep_registration_order_and_dependency_barriers() -> None:
    subject = registry().Registry()
    first = SampleStep("first")
    second = SampleStep("second")
    third = SampleStep("third", frozenset({"first"}))
    fourth = SampleStep("fourth", frozenset({"second", "third"}))
    for step in (first, second, third, fourth):
        subject.register(step)
    assert subject.waves() == ((first, second), (third,), (fourth,))
    assert subject.waves() == ((first, second), (third,), (fourth,))
    assert subject.steps() == (first, second, third, fourth)


def test_a_step_has_no_order_field() -> None:
    step_module = import_module("scripts.tick.step")
    assert "order" not in step_module.Step.__dict__
    assert "reads" in step_module.Step.__dict__


def test_unordered_reader_and_writer_refused() -> None:
    subject = registry().Registry()
    subject.register(SampleStep("a", writes=frozenset({"tasks"})))
    subject.register(SampleStep("b", reads=frozenset({"tasks", "agents"})))
    with pytest.raises(ValueError) as error:
        subject.validate()
    assert str(error.value) == "Unordered reader and writer in lane local for tasks: a, b"


def test_a_writer_read_first_is_refused_in_either_position() -> None:
    subject = registry().Registry()
    subject.register(SampleStep("a", reads=frozenset({"swarm"})))
    subject.register(SampleStep("b", writes=frozenset({"swarm"})))
    with pytest.raises(ValueError) as error:
        subject.steps()
    assert str(error.value) == "Unordered reader and writer in lane local for swarm: a, b"


def test_ordered_reader_and_writer_or_other_lanes_accepted() -> None:
    subject = registry().Registry()
    writer = SampleStep("a", writes=frozenset({"tasks"}))
    reader = SampleStep("b", frozenset({"a"}), reads=frozenset({"tasks"}))
    remote = SampleStep("c", reads=frozenset({"tasks"}), lane="remote")
    for step in (writer, reader, remote):
        subject.register(step)
    assert subject.waves() == ((writer, remote), (reader,))


def test_ordered_puts_each_step_after_the_steps_it_needs() -> None:
    late = SampleStep("late", frozenset({"early", "elsewhere"}))
    early = SampleStep("early")
    free = SampleStep("free")
    assert registry().ordered([late, early, free]) == (early, late, free)
    assert registry().ordered([free, early]) == (free, early)
    assert registry().ordered([]) == ()


def test_ordered_refuses_a_cycle() -> None:
    a, b = SampleStep("a", frozenset({"b"})), SampleStep("b", frozenset({"a"}))
    with pytest.raises(ValueError) as error:
        registry().ordered([a, b, SampleStep("c")])
    assert str(error.value) == "Dependency cycle: a, b"


def test_module_steps_reads_the_shared_registry() -> None:
    module = registry()
    assert module.steps() == module.registry.steps()


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
