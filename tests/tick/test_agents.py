"""The agent steps run the tick's hand written agent calls from the registry, in legacy order."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import tick as tick_module
from scripts.tick import engine, registry
from scripts.tick.adapters.calls import LedgerCalls, RuntimeCalls
from scripts.tick.adapters.fakes import fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.engine import StepRecord
from scripts.tick.step import Context, Domain, Lane, Result, Write
from scripts.tick.steps import AGENTS, PASS, TASKS, CallStep, actions, actions_of, agents
from scripts.tick.steps import agents as agent_steps

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


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
    return Context(_State(state), Deadline.after(60.0), fake_tools())


def _fakes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    seen: list[tuple[str, tuple[Any, ...]]] = []

    def fake(name: str, result: list[str] | None) -> Any:
        def call(*args: Any) -> list[str] | None:
            seen.append((name, args))
            return result

        call.__qualname__ = name
        return call

    monkeypatch.setattr(agent_steps, "_reap", fake("reap", ["reaped"]))
    monkeypatch.setattr(tick_module.resume, "recover_outages", fake("recover", ["recovered"]))
    monkeypatch.setattr(agent_steps, "_strays", fake("strays", ["stray"]))
    monkeypatch.setattr(tick_module.failed_panes, "sweep", fake("panes", ["pane"]))
    monkeypatch.setattr(agent_steps, "_conversations", fake("conversations", None))
    monkeypatch.setattr(agent_steps, "_session_models", fake("models", ["model"]))
    monkeypatch.setattr(tick_module.transfers, "observe", fake("transfers", ["transfer"]))
    return seen


def test_the_agent_steps_sit_in_their_groups_in_legacy_order() -> None:
    assert [step.name for step in engine.group_steps("agents")] == [
        "reap",
        "recover outages",
        "strays",
        "failed panes",
    ]
    assert [step.name for step in engine.group_steps("tasks")] == []
    assert [step.name for step in engine.group_steps("close")][:3] == [
        "conversations",
        "session models",
        "handoff transfers",
    ]
    assert len(agents.STEPS) == 7


def test_the_agents_group_runs_its_calls_with_the_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fakes(monkeypatch)
    state = _state()
    ctx = _ctx(state)
    found = actions(engine.run_group("agents", ctx))
    assert found == ["reaped", "recovered", "stray", "pane"]
    handed = seen[0][1][-1]
    assert isinstance(handed, Deadline)
    assert handed.expires_at <= ctx.deadline.expires_at
    ledger, runtime = seen[0][1][2:4]
    assert isinstance(ledger, LedgerCalls)
    assert isinstance(runtime, RuntimeCalls)
    assert (ledger.deadline, runtime.deadline) == (handed, handed)
    assert seen == [
        ("reap", ("sw", "store", ledger, runtime, {"t1": {}}, 7, handed)),
        ("recover", ("sw", "store", "ledger", state["runtime"], {"t1": {}}, 7)),
        ("strays", ("sw", state["config"], "store", state["runtime"])),
        ("panes", ("sw", "store", state["runtime"], 7)),
    ]


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
    step = CallStep("reap", "agents", lambda tick, get: None)
    assert (step.name, step.group) == ("reap", "agents")
    assert not hasattr(step, "order")
    assert (step.lane, step.budget_s, step.interval_s) == (Lane("tick"), 22.0, 60.0)
    assert (step.needs, step.reads, step.writes) == (frozenset(), frozenset(), frozenset({Domain("actions:reap")}))
    declared = CallStep(
        "reap", "agents", lambda tick, get: None, frozenset({"x"}), frozenset({AGENTS}), frozenset({TASKS})
    )
    assert (declared.needs, declared.reads) == (frozenset({"x"}), frozenset({Domain("agents")}))
    assert declared.writes == frozenset({Domain("tasks"), Domain("actions:reap")})
    assert (PASS, actions_of("probe")) == (Domain("pass"), Domain("actions:probe"))


def test_the_registered_agent_steps_declare_writers_the_registry_accepts() -> None:
    registry.validate()
    assert {domain for step in agents.STEPS for domain in step.writes} == {
        actions_of(step.name) for step in agents.STEPS
    } | {AGENTS, TASKS}
    declared = {step.name: (step.needs, step.reads, step.writes - {actions_of(step.name)}) for step in agents.STEPS}
    assert declared["reap"] == (
        agent_steps.LISTED,
        frozenset({AGENTS, Domain("pass:rows")}),
        frozenset({AGENTS, TASKS}),
    )


def test_the_tick_module_alone_registers_every_step_its_steps_need() -> None:
    probe = "import scripts.swarm.tick\nfrom scripts.tick import registry\nregistry.validate()\n"
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False, cwd=ROOT)
    assert done.returncode == 0, done.stderr


def test_a_call_step_reads_the_pass_state_and_the_tick_module() -> None:
    seen: list[tuple[Any, Any]] = []

    def call(tick: Any, get: Any) -> list[str]:
        seen.append((tick, get("slug")))
        return ["one", "two"]

    result = CallStep("strays", "agents", call).run(_ctx(_state()))
    assert result == Result((Write(actions_of("strays"), "strays", ["one", "two"]),))
    assert seen == [(tick_module, "sw")]


def test_actions_keeps_only_written_actions_in_step_order() -> None:
    records = (
        StepRecord(
            "a", 0.0, 1.0, result=Result((Write(actions_of("a"), "a", ["x"]), Write(Domain("other"), "a", ["no"])))
        ),
        StepRecord("b", 0.0, 1.0, error="boom"),
        StepRecord(
            "c", 0.0, 1.0, result=Result((Write(actions_of("c"), "c", ["y", "z"]), Write(actions_of("a"), "c", ["no"])))
        ),
    )
    assert actions(records) == ["x", "y", "z"]


def test_a_raising_call_is_recorded_failed_and_the_next_step_still_runs() -> None:
    def boom(tick: Any, get: Any) -> list[str]:
        raise RuntimeError("reap broke")

    pool = (CallStep("reap", "agents", boom), CallStep("strays", "agents", lambda tick, get: ["after"]))
    records = engine.run_group("agents", _ctx(_state()), pool)
    assert [(record.name, record.outcome, record.error) for record in records] == [
        ("reap", engine.FAILED, "builtins.RuntimeError"),
        ("strays", engine.OK, ""),
    ]
    assert actions(records) == ["after"]


def test_the_tick_returns_the_actions_its_group_steps_wrote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "registry", registry.Registry())
    registry.register(CallStep("strays", "agents", lambda tick, get: [f"saw {get('slug')} at {get('now_ms')}"]))
    assert tick_module.run_group("agents", slug="sw", now_ms=3) == ["saw sw at 3"]
    assert tick_module.run_group("masters", slug="sw") == []


def test_the_tick_prints_the_actions_of_slow_steps_that_end_after_their_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.tick import pool

    monkeypatch.setattr(registry, "registry", registry.Registry())
    registry.register(CallStep("strays", "agents", lambda tick, get: ["critical"]))
    registry.register(CallStep("failed panes", "agents", lambda tick, get: ["slow one", "slow two"], lane=engine.SLOW))
    with pool.running(workers=2):
        assert tick_module.run_group("agents", slug="sw") == ["critical"]
    assert capsys.readouterr().out == "slow one\nslow two\n"


def test_the_tick_prints_each_slow_step_that_failed_after_its_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.tick import pool

    def boom(tick: object, get: object) -> list[str]:
        raise RuntimeError("panes broke")

    monkeypatch.setattr(registry, "registry", registry.Registry())
    registry.register(CallStep("failed panes", "agents", boom, lane=engine.SLOW))
    with pool.running(workers=2):
        assert tick_module.run_group("agents", slug="sw") == []
    assert capsys.readouterr().out == "slow lane step failed panes failed: builtins.RuntimeError\n"


def test_the_tick_prints_the_exception_a_held_slow_step_stored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.tick import pool

    def hold(tick: object, get: Any) -> list[str]:
        get("box")["raised"] = RuntimeError("metrics broke")
        return []

    monkeypatch.setattr(registry, "registry", registry.Registry())
    registry.register(CallStep("failed panes", "agents", hold, lane=engine.SLOW))
    with pool.running(workers=2):
        assert tick_module.run_group("agents", slug="sw", box={}) == []
    assert capsys.readouterr().out == "a slow lane step held RuntimeError('metrics broke')\n"


def test_the_tick_prints_handed_off_records_when_no_box_was_handed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def finish(name: str, ctx: Context, apply: object = None, finished: Any = None) -> tuple[StepRecord, ...]:
        finished((StepRecord("failed panes", 0.1, 1.0),))
        return ()

    monkeypatch.setattr(tick_module.engine, "run_group", finish)
    assert tick_module.run_group("agents", slug="sw") == []
    assert capsys.readouterr().out == ""


REAP_MOVED = (
    "_restore_owner",
    "_drop_remote",
    "_remote_gone_reason",
    "_hive_lost",
    "_retire_ended",
    "_live_names",
    "_observe",
    "_elsewhere",
    "_Reaping",
    "_reap_agent",
    "_lost",
    "_watch_remote",
    "_refusal_due",
    "_free_refused",
    "_free_revoked",
    "_watch_agent",
    "_watch_idle",
    "_stale_nudge",
    "_stuck_on_provider",
    "_free_stuck",
    "_operator_at_pane",
)


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize("name", REAP_MOVED)
def test_the_reap_cluster_lives_in_the_agents_step_module_and_tick_reexports_it(name: str) -> None:
    moved = getattr(agent_steps, name)
    assert moved.__module__ == "scripts.tick.steps.agents"
    assert getattr(tick_module, name) is moved


@pytest.mark.feature("swarm-tick-agents-reap")
def test_the_reap_step_declares_its_ports_feature_and_lane() -> None:
    reap = next(step for step in agent_steps.STEPS if step.name == "reap")
    assert reap.effects == frozenset({"ledger", "runtime", "inbox"})
    assert reap.feature == "swarm-tick-agents-reap"
    assert reap.lane == Lane("tick")


@pytest.mark.feature("swarm-tick-agents-reap")
def test_a_moved_reap_helper_reaches_a_patched_tick_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(tick_module, "_watch_idle", lambda *args: seen.append(args) or ["watched"])
    monkeypatch.setattr(tick_module.revoked_seats, "on_revoked", lambda agent: False)
    agent = SimpleNamespace(name="engineer@1-1")
    assert agent_steps._watch_agent("sw", "store", "ledger", "runtime", {}, agent, 7) == ["watched"]
    assert seen == [("sw", "store", "ledger", "runtime", {}, agent, 7)]
