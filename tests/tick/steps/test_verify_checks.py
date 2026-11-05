"""The observe step's binding verification and launch checks reach tick seams for every agent they judge."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.hub import reconciler
from scripts.swarm import dead_resume, held_elsewhere, launch_check, live_binding, master_retire, reaper, tick
from scripts.tick.steps import observe

pytestmark = pytest.mark.unit


class _Recorder:
    """Record each method call into a shared list and answer from a table keyed by method name."""

    redis: "_Recorder"

    def __init__(self, calls: list[tuple[Any, ...]], answers: dict[str, Any], prefix: str = "") -> None:
        self.calls, self.answers, self.prefix = calls, answers, prefix

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((self.prefix + name, args, kwargs))
            return self.answers.get(name)

        return call


def _agent(name: str, state: str = "working") -> SimpleNamespace:
    return SimpleNamespace(name=name, task=f"task-{name}", lane="eng", state=state, profile="p")


def _verify_seams(monkeypatch: pytest.MonkeyPatch, agents: list[SimpleNamespace]) -> list[tuple[Any, ...]]:
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(launch_check, "judged", lambda store, slug: {"judged"})
    monkeypatch.setattr(reconciler, "legacy_agents", lambda store, slug: agents)
    monkeypatch.setattr(held_elsewhere, "holders", lambda store: {})
    monkeypatch.setattr(held_elsewhere, "keep", lambda *args: False)
    monkeypatch.setattr(held_elsewhere, "unseen", lambda *args: False)
    monkeypatch.setattr(dead_resume, "skip_binding", lambda *args: False)
    monkeypatch.setattr(
        tick,
        "_acknowledging",
        lambda slug, store, agent: seen.append(("ack", slug, agent.name)) or agent.name == "acking",
    )
    monkeypatch.setattr(
        tick, "_ended", lambda agent, rows: seen.append(("ended", agent.name, rows)) or agent.name == "ended"
    )

    def follow(slug: str, store: Any, runtime: Any, agent: Any, rebound: Any) -> tuple[Any, list[str]]:
        seen.append(("follow", slug, agent.name, rebound))
        return (None, [f"gone {agent.name}"]) if agent.name == "gone" else (agent, [f"followed {agent.name}"])

    monkeypatch.setattr(tick, "_follow", follow)
    monkeypatch.setattr(live_binding, "fill", lambda agent, facts: agent)
    monkeypatch.setattr(live_binding, "hosted_home", lambda store, agent: agent)
    monkeypatch.setattr(
        live_binding, "record", lambda store, slug, agent, facts, now_ms: ["pane"] if agent.name == "drifted" else []
    )
    monkeypatch.setattr(live_binding, "relaunch_assignment", lambda agent, task, config: {"task": agent.task})
    monkeypatch.setattr(master_retire, "hold", lambda *args: "")
    monkeypatch.setattr(reaper, "scratch_homes", lambda slug, task: [])
    monkeypatch.setattr(
        tick,
        "_drop",
        lambda slug, store, ledger, rows, agent, refund: (
            seen.append(("drop", slug, ledger, agent.name, refund)) or " and dropped"
        ),
    )
    return seen


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_verification_skips_judged_acknowledging_and_ended_agents_and_retires_a_drifted_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents = [_agent("judged"), _agent("acking"), _agent("ended"), _agent("gone"), _agent("steady"), _agent("drifted")]
    seen = _verify_seams(monkeypatch, agents)
    calls: list[tuple[Any, ...]] = []
    store = _Recorder(calls, {"key": "sw:launch-assignments"})
    store.redis = _Recorder(calls, {}, "redis.")
    runtime = _Recorder(calls, {"bindings": {"drifted": {"rebound": "r1"}}, "live_names": set(), "retire": True})
    rows: dict[str, Any] = {"task-drifted": {"id": "task-drifted"}}
    actions = observe._verify("sw", store, "ledger", runtime, rows, 5)
    assert actions == [
        "gone gone",
        "followed steady",
        "followed drifted",
        "retired drifted after mismatched pane and dropped",
    ]
    assert [entry for entry in seen if entry[0] == "ack"] == [
        ("ack", "sw", name) for name in ("acking", "ended", "gone", "steady", "drifted")
    ]
    assert [entry for entry in seen if entry[0] == "ended"] == [
        ("ended", name, rows) for name in ("ended", "gone", "steady", "drifted")
    ]
    assert [entry for entry in seen if entry[0] == "follow"] == [
        ("follow", "sw", "gone", None),
        ("follow", "sw", "steady", None),
        ("follow", "sw", "drifted", "r1"),
    ]
    assert [entry for entry in seen if entry[0] == "drop"] == [("drop", "sw", "ledger", "drifted", True)]
    assert calls[0][0] == "bindings" and [agent.name for agent in calls[0][1][0]] == [a.name for a in agents[1:]]
    assert ("redis.hset", ("sw:launch-assignments", "task-drifted", '{"task": "task-drifted"}'), {}) in calls


def _check_seams(monkeypatch: pytest.MonkeyPatch, waiting: dict[str, Any]) -> list[tuple[Any, ...]]:
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(launch_check, "pending", lambda store, slug: waiting)
    monkeypatch.setattr(launch_check, "forget", lambda store, slug, name: seen.append(("forget", name)))
    monkeypatch.setattr(launch_check, "canary", lambda store, slug, agent: (agent, {}))
    monkeypatch.setattr(launch_check, "joined_at", lambda agent, doc: doc.get(agent.name))
    monkeypatch.setattr(launch_check, "bundled", lambda profile: "bundled")
    monkeypatch.setattr(launch_check, "expected", lambda agent: "expected")
    monkeypatch.setattr(
        launch_check,
        "misses",
        lambda store, slug, agent, facts, doc, bundled, expected: {"pane": 1} if agent.name != "ok" else {},
    )
    monkeypatch.setattr(launch_check, "due", lambda agent, found, now_ms: agent.name == "late")
    monkeypatch.setattr(launch_check, "Miss", lambda agent, found, relaunch: ("miss", agent.name, found, relaunch))
    monkeypatch.setattr(launch_check, "session_started_at", lambda agent: 1_000)
    monkeypatch.setattr(
        launch_check,
        "record",
        lambda store, slug, agent, found, now_ms, elapsed: seen.append(("record", agent.name, elapsed)),
    )
    monkeypatch.setattr(launch_check, "clear_relaunched", lambda store, slug, task: seen.append(("clear", task)))
    monkeypatch.setattr(
        tick, "pass_state", lambda ledger, slug: seen.append(("pass_state", ledger, slug)) or {"ok": 9_000}
    )
    monkeypatch.setattr(
        tick, "_settle_admission", lambda slug, runtime, agent: seen.append(("settle", slug, runtime, agent.name))
    )
    monkeypatch.setattr(
        tick,
        "_failed_launch",
        lambda slug, store, ledger, runtime, rows, miss, now_ms: f"failed {slug} {ledger} {runtime} {miss} {now_ms}",
    )
    return seen


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_launch_checks_pass_fail_or_wait_each_pending_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = {name: {"relaunch": name == "late"} for name in ("ok", "late", "early", "vanished")}
    seen = _check_seams(monkeypatch, waiting)
    store = SimpleNamespace(agents=lambda slug: [_agent("ok"), _agent("late"), _agent("early")])
    runtime = SimpleNamespace(bindings=lambda agents: {})
    actions = observe._launch_checks("sw", store, "ledger", runtime, {}, {}, 20_000)
    assert actions == [
        "ok passed its launch check in 8 seconds",
        f"failed sw ledger {runtime} ('miss', 'late', {{'pane': 1}}, True) 20000",
    ]
    assert seen == [
        ("forget", "vanished"),
        ("pass_state", "ledger", "sw"),
        ("record", "ok", 8_000),
        ("forget", "ok"),
        ("clear", "task-ok"),
        ("settle", "sw", runtime, "ok"),
    ]


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_launch_checks_with_nothing_pending_do_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _check_seams(monkeypatch, {})
    assert observe._launch_checks("sw", SimpleNamespace(), "ledger", SimpleNamespace(), {}, {}, 1) == []
    assert seen == []
