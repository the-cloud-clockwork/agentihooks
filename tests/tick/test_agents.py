"""The agent steps run the tick's hand written agent calls from the registry, in legacy order."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import tick as tick_module
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.engine import StepRecord
from scripts.tick.step import Context, Domain, Lane, Result, Write
from scripts.tick.steps import ACTIONS, BUDGET_S, LANE, PASS, CallStep, actions, agents

pytestmark = pytest.mark.unit


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


class _Runtime:
    def live_names(self) -> set[str]:
        return {"engineer@a1b2c3-0001"}


def _state(engine_name: str = "tick") -> dict[str, object]:
    return {
        "slug": "sw",
        "config": SimpleNamespace(engine=engine_name),
        "store": "store",
        "ledger": "ledger",
        "runtime": _Runtime(),
        "rows": {"t1": {}},
        "now_ms": 7,
    }


def _ctx(state: dict[str, object]) -> Context:
    return Context(_State(state), Deadline.after(60.0))


def _fakes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    seen: list[tuple[str, tuple[Any, ...]]] = []

    def fake(name: str, result: list[str] | None) -> Any:
        def call(*args: Any) -> list[str] | None:
            seen.append((name, args))
            return result

        call.__qualname__ = name
        return call

    monkeypatch.setattr(tick_module, "_reap", fake("reap", ["reaped"]))
    monkeypatch.setattr(tick_module.resume, "recover_outages", fake("recover", ["recovered"]))
    monkeypatch.setattr(tick_module, "_strays", fake("strays", ["stray"]))
    monkeypatch.setattr(tick_module.failed_panes, "sweep", fake("panes", ["pane"]))
    monkeypatch.setattr(tick_module, "_orphans", fake("orphans", ["orphan"]))
    monkeypatch.setattr(tick_module, "_conversations", fake("conversations", None))
    monkeypatch.setattr(tick_module, "_session_models", fake("models", ["model"]))
    monkeypatch.setattr(tick_module.transfers, "observe", fake("transfers", ["transfer"]))
    return seen


def test_the_agent_steps_sit_in_their_groups_in_legacy_order() -> None:
    assert [step.name for step in engine.group_steps("agents")] == [
        "reap",
        "recover outages",
        "strays",
        "failed panes",
    ]
    assert [step.name for step in engine.group_steps("tasks")][:1] == ["orphans"]
    assert [step.name for step in engine.group_steps("close")] == [
        "conversations",
        "session models",
        "handoff transfers",
    ]
    assert len(agents.STEPS) == 8


def test_the_agents_group_runs_its_calls_with_the_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fakes(monkeypatch)
    state = _state()
    found = actions(engine.run_group("agents", _ctx(state)))
    assert found == ["reaped", "recovered", "stray", "pane"]
    assert seen == [
        ("reap", ("sw", "store", "ledger", state["runtime"], {"t1": {}}, 7)),
        ("recover", ("sw", "store", "ledger", state["runtime"], {"t1": {}}, 7)),
        ("strays", ("sw", state["config"], "store", state["runtime"])),
        ("panes", ("sw", "store", state["runtime"], 7)),
    ]


def test_the_orphans_step_runs_outside_the_events_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fakes(monkeypatch)
    step = engine.group_steps("tasks")[0]
    assert step.run(_ctx(_state())) == Result((Write(ACTIONS, "orphans", ["orphan"]),))
    assert seen == [("orphans", ("sw", "store", "ledger", {"t1": {}}))]


def test_the_orphans_step_is_quiet_under_the_events_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fakes(monkeypatch)
    step = engine.group_steps("tasks")[0]
    assert step.run(_ctx(_state(tick_module.EVENTS_ENGINE))) == Result((Write(ACTIONS, "orphans", []),))
    assert seen == []


def test_the_close_group_keeps_sessions_and_transfers_current(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fakes(monkeypatch)
    state = _state()
    assert actions(engine.run_group("close", _ctx(state))) == ["model", "transfer"]
    assert seen == [
        ("conversations", ("sw", "store", state["runtime"])),
        ("models", ("sw", "store")),
        ("transfers", ("store", "sw", {"engineer@a1b2c3-0001"})),
    ]


def test_a_call_step_is_placed_and_bounded_as_registered() -> None:
    step = CallStep("probe", "agents", 5, lambda tick, get: None)
    assert (step.name, step.group, step.order) == ("probe", "agents", 5)
    assert (step.lane, step.budget_s, step.interval_s) == (Lane("tick"), 600.0, 0.0)
    assert (step.needs, step.writes) == (frozenset(), frozenset())
    assert (LANE, BUDGET_S, PASS, ACTIONS) == (Lane("tick"), 600.0, Domain("pass"), Domain("actions"))


def test_a_call_step_reads_the_pass_state_and_the_tick_module() -> None:
    seen: list[tuple[Any, Any]] = []

    def call(tick: Any, get: Any) -> list[str]:
        seen.append((tick, get("slug")))
        return ["one", "two"]

    result = CallStep("probe", "agents", 0, call).run(_ctx(_state()))
    assert result == Result((Write(ACTIONS, "probe", ["one", "two"]),))
    assert seen == [(tick_module, "sw")]


def test_actions_keeps_only_written_actions_in_step_order() -> None:
    records = (
        StepRecord("a", 0.0, 1.0, result=Result((Write(ACTIONS, "a", ["x"]), Write(Domain("other"), "a", ["no"])))),
        StepRecord("b", 0.0, 1.0, error="boom"),
        StepRecord("c", 0.0, 1.0, result=Result((Write(ACTIONS, "c", ["y", "z"]),))),
    )
    assert actions(records) == ["x", "y", "z"]


def test_the_tick_returns_the_actions_its_group_steps_wrote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "registry", registry.Registry())
    registry.register(CallStep("probe", "agents", 0, lambda tick, get: [f"saw {get('slug')} at {get('now_ms')}"]))
    assert tick_module.run_group("agents", slug="sw", now_ms=3) == ["saw sw at 3"]
    assert tick_module.run_group("masters", slug="sw") == []
