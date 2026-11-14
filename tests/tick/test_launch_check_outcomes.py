"""Direct coverage of observe._failed_hive_launch and observe._failed_launch's retry-next-tick branches."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.hive import reports
from scripts.swarm import idle as idle_state
from scripts.swarm import launch_check, master_retire, tick
from scripts.swarm.store import AgentRecord
from scripts.tick.steps import observe


def test_a_failed_hive_launch_that_cannot_retire_retries_next_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tick, "_retire_launch", lambda *args: calls.append(("retire", args)) or False)
    agent = AgentRecord("engineer@1-1", "eng", "t1")
    miss = SimpleNamespace(agent=agent, found={"pane": 1}, relaunch=True)

    said = observe._failed_hive_launch("sw", "store", "ledger", "runtime", {}, miss, 5_000)

    assert said == "could not retire engineer@1-1 after its hive join failed, retrying next tick"
    assert calls == [("retire", ("sw", "store", "runtime", agent, 5_000))]


def test_a_failed_launch_check_on_a_working_agent_is_only_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(launch_check, "session_started_at", lambda agent: 1_000)
    monkeypatch.setattr(launch_check, "record", lambda *args, **kwargs: calls.append(("record", args, kwargs)))
    monkeypatch.setattr(launch_check, "forget", lambda *args: calls.append(("forget", args)))
    monkeypatch.setattr(idle_state, "heartbeat", lambda redis, slug, name: ("beat", slug, name))
    monkeypatch.setattr(idle_state, "verdict", lambda pane, beat, wait, now_ms: idle_state.WORKING)
    store = SimpleNamespace(redis="redis")
    agent = AgentRecord("engineer@1-1", "eng", "t1")
    miss = SimpleNamespace(agent=agent, found={"pane": 1, "model": 2}, relaunch=True)

    said = observe._failed_launch("sw", store, "ledger", "runtime", {}, miss, 5_000)

    assert said == "engineer@1-1 failed its launch check on pane, model; reported only while working"
    assert calls == [
        ("record", (store, "sw", agent, {"pane": 1, "model": 2}, 5_000, 4_000), {"held": True}),
        ("forget", (store, "sw", "engineer@1-1")),
    ]


def test_a_local_launch_failure_that_cannot_retire_retries_next_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(launch_check, "session_started_at", lambda agent: 1_000)
    monkeypatch.setattr(idle_state, "heartbeat", lambda redis, slug, name: ("beat", slug, name))
    monkeypatch.setattr(idle_state, "verdict", lambda pane, beat, wait, now_ms: idle_state.IDLE)
    monkeypatch.setattr(reports, "host", lambda agent: "")
    monkeypatch.setattr(launch_check, "relaunched", lambda store, slug, task: False)
    monkeypatch.setattr(master_retire, "hold", lambda *args: "")
    monkeypatch.setattr(tick, "_retire_launch", lambda *args: calls.append(("retire", args)) or False)
    store = SimpleNamespace(redis="redis")
    runtime = SimpleNamespace(live_names=lambda: set())
    agent = AgentRecord("engineer@1-1", "eng", "t1")
    miss = SimpleNamespace(agent=agent, found={"pane": 1}, relaunch=True)

    result = observe._failed_launch("sw", store, "ledger", runtime, {}, miss, 5_000)

    assert result == "could not retire engineer@1-1 after its launch check failed on pane, retrying next tick"
    assert calls == [("retire", ("sw", store, runtime, agent, 5_000))]
