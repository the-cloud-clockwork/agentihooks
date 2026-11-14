"""Tick steps reach outside systems only through the ports their declared effects name."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest

import scripts.swarm.cli as cli
from scripts.swarm import controller, dispatcher, metrics_outbox
from scripts.tick import engine, pool, registry
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


@pytest.mark.feature("swarm-tick-declared-effects")
def test_a_step_sends_through_its_declared_inbox() -> None:
    tools = fakes.fake_tools()
    record = _run(EffectStep(frozenset({"inbox"})), tools)
    assert (record.outcome, record.error) == (engine.OK, "")
    assert [call.operation for call in tools.inbox.calls] == ["send"]  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.feature("swarm-tick-declared-effects")
def test_a_ledger_write_outside_the_declared_effects_goes_red() -> None:
    tools = fakes.fake_tools()
    record = _run(EffectStep(frozenset({"inbox"}), mutate=True), tools)
    assert (record.outcome, record.error) == (engine.FAILED, REFUSED)
    assert [call.operation for call in tools.inbox.calls] == ["send"]  # pyright: ignore[reportAttributeAccessIssue]
    assert tools.ledger.calls == []  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.feature("swarm-tick-declared-effects")
def test_a_step_without_effects_cannot_write() -> None:
    tools = fakes.fake_tools()
    record = _run(EffectStep(frozenset()), tools)
    assert record.error == REFUSED
    assert tools.inbox.calls == []  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.feature("swarm-tick-declared-effects")
def test_a_context_built_inside_a_pass_carries_the_pass_tools() -> None:
    tools = fakes.fake_tools()
    assert Context(View(), Deadline.after(1.0)).tools is None
    token = PASS_TOOLS.set(tools)
    try:
        assert Context(View(), Deadline.after(1.0)).tools is tools
    finally:
        PASS_TOOLS.reset(token)


@pytest.mark.feature("swarm-tick-declared-effects")
def test_call_and_write_steps_declare_their_effects_and_feature() -> None:
    name = "last tick"
    assert (CallStep(name, "close", _nothing).effects, CallStep(name, "close", _nothing).feature) == (frozenset(), "")
    step = CallStep(name, "close", _nothing, effects=frozenset({"inbox"}), feature=FEATURE)
    assert (step.effects, step.feature) == (frozenset({"inbox"}), FEATURE)
    written = WriteStep(name, "close", _nothing, effects=frozenset({"metrics"}), feature=FEATURE)
    assert (written.effects, written.feature) == (frozenset({"metrics"}), FEATURE)
    assert WriteStep(name, "close", _nothing).effects == frozenset()


@pytest.mark.feature("swarm-tick-declared-effects")
def test_only_the_reap_and_spawn_steps_declare_their_effects_yet() -> None:
    declared = {step.name: step.effects for step in registry.steps() if step.effects}
    assert declared == {
        "reap": frozenset({"ledger", "runtime", "inbox"}),
        "hive sweep": frozenset({"coordination"}),
        "workflow runner": frozenset({"host"}),
        "knowledge warm": frozenset({"host"}),
        "doc items map": frozenset({"ledger", "coordination"}),
        "step starvation": frozenset({"inbox"}),
        "progress heads": frozenset({"source_control"}),
        "lab capacity": frozenset({"metrics"}),
    }


@pytest.mark.feature("swarm-tick-declared-effects")
def test_a_step_of_a_parallel_pass_reaches_its_declared_ports() -> None:
    tools = fakes.fake_tools()
    step = EffectStep(frozenset({"inbox"}))
    with pool.running(workers=2):
        records = engine.run_group(step.group, Context(View(), Deadline.after(60.0), tools), [step])  # pyright: ignore[reportArgumentType]
    assert [(record.outcome, record.error) for record in records] == [(engine.OK, "")]
    assert [call.operation for call in tools.inbox.calls] == ["send"]  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.feature("swarm-tick-declared-effects")
def test_the_pass_spool_opens_once_and_appends_every_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[tuple[object, object]] = []
    appended: list[tuple[object, object]] = []

    class Box:
        def __init__(self, path: object, sink: object) -> None:
            opened.append((path, sink))

        def append_many(self, batches: object, checkpoint: object) -> None:
            appended.append((batches, checkpoint))

    def settings(environ: object) -> tuple[str, bool]:
        return "sink", environ is os.environ

    monkeypatch.setattr(metrics_outbox, "Outbox", Box)
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: "spool")
    monkeypatch.setattr(metrics_outbox, "settings", settings)
    first: Callable[..., None] = print
    spool = cli._PassSpool()  # pyright: ignore[reportPrivateUsage]
    spool.append_many([("rows", [{"a": 1}])], first)
    spool.append_many([], first)
    assert opened == [("spool", ("sink", True))]
    assert appended == [([("rows", [{"a": 1}])], first), ([], first)]


def _recorder(name: str) -> Callable[..., tuple[object, ...]]:
    def build(*args: object, **kwargs: object) -> tuple[object, ...]:
        return name, args, tuple(sorted(kwargs.items()))

    return build


@pytest.mark.feature("swarm-tick-declared-effects")
def test_the_pass_tools_wire_every_real_adapter_for_the_swarm(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ClientLedger",
        "resource_client",
        "StoreCoordination",
        "GitHubSourceControl",
        "RouterRuntime",
        "PassProcesses",
        "StoreInbox",
        "InboxStore",
        "ApiLlm",
        "SpoolMetrics",
        "local_host",
    ):
        monkeypatch.setattr(cli, name, _recorder(name))
    monkeypatch.setattr(cli, "Path", SimpleNamespace(cwd=lambda: "cwd"))
    monkeypatch.setattr(cli, "routed", lambda environ: SimpleNamespace(router=("router", environ)))
    monkeypatch.setattr(cli, "_PassSpool", lambda: "spool")
    monkeypatch.setattr(cli.classifier_settings, "load", lambda: SimpleNamespace(models=["first", "second"], url="url"))
    records = [SimpleNamespace(name="eng-1"), SimpleNamespace(name="eng-2")]
    store = SimpleNamespace(redis="redis", agents=lambda slug: records if slug == "sw" else [])
    tools = cli._pass_tools(store, "sw", "owner")  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    assert tools.ledger == ("ClientLedger", ("sw", ("resource_client", ("sw",), (("service", True),))), ())
    assert tools.coordination == ("StoreCoordination", (store, "sw", "owner"), ())
    assert tools.source_control == ("GitHubSourceControl", ("", "cwd", "dev"), ())
    assert tools.processes == ("PassProcesses", (), ())
    assert tools.inbox == ("StoreInbox", (("InboxStore", ("redis",), ()), "tick@sw"), ())
    assert tools.llm == ("ApiLlm", ("first", "url"), ())
    assert tools.metrics == ("SpoolMetrics", ("spool", {"dispatch_actions": dispatcher.TABLE}), ())
    assert tools.host == ("local_host", (store, "sw", os.environ), ())
    runtime: tuple[str, tuple[object, Callable[[str], object]], tuple[()]] = tools.runtime  # pyright: ignore[reportAssignmentType]
    assert runtime[1][0] == ("router", os.environ)
    keywords: dict[str, Any] = dict(cast(Any, runtime[2]))
    assert sorted(keywords) == ["fencing", "legacy"]
    assert keywords["legacy"] is None
    made: list[tuple[object, ...]] = []
    monkeypatch.setattr(controller, "FencedRuntime", lambda *args: made.append(args) or "fenced")
    assert keywords["fencing"]("held", "local", "raw") == "fenced"
    assert made == [(store, "sw", "held", "raw", "local")]
    agent = runtime[1][1]
    assert (agent("eng-2"), agent("eng-1"), agent("eng-9")) == (records[1], records[0], None)
