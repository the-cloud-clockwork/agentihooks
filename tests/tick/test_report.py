"""The report steps run the tick wrapper's event, watch, findings, metrics, wake, snapshot, heartbeat and status calls."""

import os
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.gates import progress, quiet
from scripts.inbox import wake
from scripts.swarm import (
    bottleneck_status,
    dispatcher,
    doc_report,
    done_gate,
    ledger_events,
    metrics,
    overrun,
    ready,
    reaper,
    release_wait,
    snapshot_job,
    stall_watch,
    tick_heartbeat,
    timing,
    waits,
)
from scripts.swarm.status import findings
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import PASS, CallStep, actions, budgets, host, report

pytestmark = pytest.mark.unit

STORE = SimpleNamespace(redis="redis")
DOC = {"tasks": ["t"], "_meta": {"events": ["e"]}}
CONFIG = SimpleNamespace(repo="/repo", watchdog_address="wd")
AGENTS = [SimpleNamespace(name="b", runtime_backend="remote"), SimpleNamespace(name="c", runtime_backend="local")]


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


class _Runtime:
    def __init__(self) -> None:
        self.retired: list[tuple[object, object]] = []

    def live_names(self) -> set[str]:
        return {"a"}

    def retire(self, agent: object, homes: object) -> None:
        self.retired.append((agent, homes))


def _pass(box: dict[str, Any] | None = None, **extra: object) -> dict[str, Any]:
    state: dict[str, Any] = {
        "slug": "sw",
        "store": STORE,
        "ledger": "ledger",
        "runtime": _Runtime(),
        "inbox": "inbox",
        "doc": DOC,
        "config": CONFIG,
        "view": "view",
        "rows": "rows",
        "agents": AGENTS,
        "box": {} if box is None else box,
    }
    return state | extra


def _run(group: str, calls: list[tuple[Any, ...]], state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    def skip_refused(function: Any, *args: Any) -> list[str]:
        calls.append(("skip", function, *args))
        return [f"ran {len(calls)}"]

    def call(function: Any, *args: Any) -> list[str]:
        calls.append(("call", function, *args))
        return [f"ran {len(calls)}"]

    cli = SimpleNamespace(
        now_ms=lambda: 7,
        skip_refused=skip_refused,
        _publish_tick_status=lambda *args: calls.append(("publish", *args)),
        wake=wake,
        snapshot_job=snapshot_job,
    )
    monkeypatch.setattr(host, "import_module", lambda name: cli)
    monkeypatch.setattr(timing, "call", call)
    monkeypatch.setattr(ready, "Pass", lambda *args: ("pass", *args))
    monkeypatch.setattr(stall_watch, "Watch", lambda *args, **kwargs: ("stall watch", args, kwargs))
    monkeypatch.setattr(stall_watch, "ready_lanes", lambda *args: ("lanes", *args))
    monkeypatch.setattr(release_wait, "checker", lambda repo: ("checker", repo))
    monkeypatch.setattr(overrun, "Watch", lambda *args: ("overrun watch", *args))
    monkeypatch.setattr(metrics.metrics_swarm, "TickInput", lambda *args: ("input", *args))
    monkeypatch.setattr(wake, "window_ms", lambda environ: ("window", environ is os.environ))
    monkeypatch.setattr(reaper, "scratch_homes", lambda slug, task: ("homes", slug, task))
    monkeypatch.setattr(tick_heartbeat, "write", lambda *args: calls.append(("heartbeat", *args)))
    return actions(engine.run_group(group, Context(_State(state), Deadline.after(60.0))))


def test_the_report_steps_sit_at_their_sites_in_legacy_order() -> None:
    assert [(step.name, step.group, step.order) for step in report.STEPS] == [
        ("ledger event pass", "wrapper events", 10),
        ("done recheck", "wrapper events", 20),
        ("progress checks", "wrapper events", 30),
        ("waits end", "wrapper events", 40),
        ("merge pass", "wrapper events", 50),
        ("stall watch", "wrapper events", 60),
        ("overrun pass", "wrapper events", 70),
        ("quiet pass", "wrapper events", 80),
        ("doc report", "wrapper events", 90),
        ("priorities sweep", "wrapper events", 100),
        ("health findings", "wrapper events", 110),
        ("findings pass", "wrapper events", 120),
        ("metrics record", "wrapper metrics", 10),
        ("bottleneck status", "wrapper metrics", 20),
        ("inbox wake", "wrapper metrics", 30),
        ("snapshot job", "wrapper metrics", 40),
        ("last tick", "wrapper metrics", 50),
        ("heartbeat", "after", 10),
        ("status publish", "wrapper status", 10),
    ]
    registered = [step for wave in registry.waves() for step in wave]
    for step in report.STEPS:
        assert step in registered
        assert (step.lane, step.budget_s) == (CallStep.lane, budgets.timing_of(step.name)[0])


def test_the_event_steps_run_their_passes_with_the_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {}
    found = _run("wrapper events", calls, _pass(box), monkeypatch)
    assert found == [f"ran {n}" for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12)]
    assert calls[:5] == [
        ("skip", ledger_events.event_pass, "inbox", STORE, "sw", DOC, "ledger", 7, "view"),
        ("skip", done_gate.recheck_pass, STORE, "sw", DOC, "ledger", 7, "view"),
        ("skip", progress.checks_pass, "redis", "sw", ["t"], "view", 7),
        ("skip", waits.end_pass, STORE, "sw", "rows", "inbox", "view", 7, ledger_events.view),
        ("skip", ready.merge_pass, ("pass", STORE, "sw", "ledger", "view", ledger_events.view, 7), "rows"),
    ]
    assert calls[7:] == [
        ("skip", quiet.quiet_pass, STORE, "sw", "rows", 7),
        ("skip", doc_report.report_pass, "inbox", "ledger", "sw", ["t"], 7, "wd"),
        ("skip", dispatcher.priorities, STORE, "sw", DOC, "ledger", "view", 7),
        ("call", findings, STORE, "sw", CONFIG, ["t"], ["e"], "view"),
        ("skip", ledger_events.findings_pass, "inbox", STORE, "sw", ["ran 11"], DOC),
    ]
    assert box == {"live": {"a", "b"}, "found": ["ran 11"]}


def test_the_health_findings_read_no_tasks_or_events_from_an_empty_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(timing, "call", lambda function, *args: calls.append((function, *args)) or ["f"])
    box: dict[str, Any] = {}
    assert report._health_findings(SimpleNamespace(), _pass(box, doc={}).get) is None  # pyright: ignore[reportPrivateUsage]
    assert calls == [(findings, STORE, "sw", CONFIG, [], [], "view")]
    assert box == {"found": ["f"]}


def test_the_stall_and_overrun_watches_share_the_live_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    state = _pass()
    _run("wrapper events", calls, state, monkeypatch)
    stall, overrun_call = calls[5], calls[6]
    assert (stall[0], stall[1], stall[3]) == ("skip", stall_watch.stall_pass, DOC)
    name, args, kwargs = stall[2]
    assert name == "stall watch"
    assert args[:5] == (STORE, "sw", "ledger", "view", frozenset({"a", "b"}))
    assert args[6:] == (("lanes", "sw", STORE, "ledger", DOC, 7), 7)
    assert kwargs == {"released": ("checker", "/repo"), "reread": ledger_events.view}
    assert (overrun_call[0], overrun_call[1], overrun_call[3]) == ("skip", overrun.overrun_pass, DOC)
    watch = overrun_call[2]
    assert watch[:5] == ("overrun watch", STORE, "sw", "ledger", frozenset({"a", "b"}))
    assert watch[6] == 7
    agent = SimpleNamespace(task="t9")
    args[5](agent)
    watch[5](agent)
    assert state["runtime"].retired == [(agent, ("homes", "sw", "t9")), (agent, ("homes", "sw", "t9"))]


def test_the_metrics_steps_record_wake_and_start_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {"found": ["f"]}
    redis = SimpleNamespace(set=lambda key, value: calls.append(("set", key, value)))
    store = SimpleNamespace(redis=redis, key=lambda slug, mark: f"{slug}/{mark}")
    found = _run("wrapper metrics", calls, _pass(box, store=store, count=5), monkeypatch)
    assert found == ["ran 1", "ran 2", "ran 3"]
    assert calls == [
        ("call", metrics.record_pass, "sw", 7, 5, os.environ, ("input", store, DOC, ["f"], "view", "ledger")),
        ("skip", bottleneck_status.refresh, "sw", store, 7),
        ("skip", wake.maintenance, "inbox", "sw", AGENTS, 7, ("window", True)),
        ("call", snapshot_job.start, store, "sw", 7, os.environ),
        ("set", "sw/last-tick", 7),
    ]
    assert box == {"found": ["f"], "started": ["ran 4"]}


def test_the_watches_and_findings_declare_the_steps_they_need() -> None:
    needs = {step.name: step.needs for step in report.STEPS if step.needs}
    assert needs == {
        "overrun pass": frozenset({"stall watch"}),
        "findings pass": frozenset({"health findings"}),
        "metrics record": frozenset({"health findings"}),
    }
    assert {step.needs for step in host.STEPS} == {frozenset()}
    assert CallStep("heartbeat", "g", 1, host.held(lambda cli, get: None)).needs == frozenset()


def test_the_heartbeat_runs_after_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run("after", calls, _pass(), monkeypatch) == []
    assert calls == [("heartbeat", STORE, "sw", 7, DOC)]


def test_the_status_publish_hands_over_the_doc_and_view(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    failure = RuntimeError("tick failed")
    assert _run("wrapper status", calls, _pass(failure=failure), monkeypatch) == []
    assert calls == [("publish", STORE, "sw", DOC, "view", failure)]
