"""Tick steps reach outside systems only through the ports their declared effects name."""

from dataclasses import dataclass

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.tick import engine, registry
from scripts.tick.adapters import fakes
from scripts.tick.deadline import Deadline
from scripts.tick.ports.tools import PASS_TOOLS
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.steps import CallStep
from scripts.tick.steps.tasks import WriteStep

pytestmark = pytest.mark.unit

FEATURE = "swarm-tick-declared-effects"
REFUSED = "scripts.tick.ports.contracts.UndeclaredEffect"


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@dataclass(frozen=True)
class EffectStep:
    effects: frozenset[str]
    mutate: bool = False
    name: str = "effect"
    group: str = "place"
    lane: Lane = Lane("tick")
    budget_s: float = 5.0
    interval_s: float = 60.0
    needs: frozenset[str] = frozenset()
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    feature: str = FEATURE

    def run(self, ctx: Context) -> Result:
        assert ctx.tools is not None
        ctx.tools.inbox.send("eng-1@sw", "wake", deadline=ctx.deadline)
        if self.mutate:
            ctx.tools.ledger.mutate([], deadline=ctx.deadline)
        return Result()


def _run(step: EffectStep, tools: object) -> engine.StepRecord:
    return engine.run_step(step, Context(View(), Deadline.after(60.0), tools))  # pyright: ignore[reportArgumentType]


def _nothing(tick: object, read: object) -> list[str]:
    return []


@pytest.mark.feature(FEATURE)
def test_a_step_sends_through_its_declared_inbox() -> None:
    tools = fakes.fake_tools()
    record = _run(EffectStep(frozenset({"inbox"})), tools)
    assert (record.outcome, record.error) == (engine.OK, "")
    assert [call.operation for call in tools.inbox.calls] == ["send"]  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.feature(FEATURE)
def test_a_ledger_write_outside_the_declared_effects_goes_red() -> None:
    tools = fakes.fake_tools()
    record = _run(EffectStep(frozenset({"inbox"}), mutate=True), tools)
    assert (record.outcome, record.error) == (engine.FAILED, REFUSED)
    assert [call.operation for call in tools.inbox.calls] == ["send"]  # pyright: ignore[reportAttributeAccessIssue]
    assert tools.ledger.calls == []  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.feature(FEATURE)
def test_a_step_without_effects_cannot_write() -> None:
    tools = fakes.fake_tools()
    record = _run(EffectStep(frozenset()), tools)
    assert record.error == REFUSED
    assert tools.inbox.calls == []  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.feature(FEATURE)
def test_a_context_built_inside_a_pass_carries_the_pass_tools() -> None:
    tools = fakes.fake_tools()
    assert Context(View(), Deadline.after(1.0)).tools is None
    token = PASS_TOOLS.set(tools)
    try:
        assert Context(View(), Deadline.after(1.0)).tools is tools
    finally:
        PASS_TOOLS.reset(token)


@pytest.mark.feature(FEATURE)
def test_call_and_write_steps_declare_their_effects_and_feature() -> None:
    name = "last tick"
    assert (CallStep(name, "close", _nothing).effects, CallStep(name, "close", _nothing).feature) == (frozenset(), "")
    step = CallStep(name, "close", _nothing, effects=frozenset({"inbox"}), feature=FEATURE)
    assert (step.effects, step.feature) == (frozenset({"inbox"}), FEATURE)
    written = WriteStep(name, "close", _nothing, effects=frozenset({"metrics"}), feature=FEATURE)
    assert (written.effects, written.feature) == (frozenset({"metrics"}), FEATURE)
    assert WriteStep(name, "close", _nothing).effects == frozenset()


@pytest.mark.feature(FEATURE)
def test_every_registered_step_declares_no_effects_yet() -> None:
    assert {step.name: step.effects for step in registry.steps() if step.effects} == {}
