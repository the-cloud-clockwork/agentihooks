from typing import Any
from unittest.mock import Mock

import pytest

from scripts.swarm import deliberate_exit, retire_watch, tick
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick.steps import agents
from tests.swarm.test_tick import FakeRuntime, tasks


def _lost_agent(
    monkeypatch: pytest.MonkeyPatch, lane: str = "master"
) -> tuple[RedisStore, Any, Any, AgentRecord, Mock]:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    ledger: Any = tasks(("t1", "eng"))
    runtime: Any = FakeRuntime()
    name = store.next_name("sw", lane)
    task = "master" if lane == "master" else "t1"
    agent = AgentRecord(name, lane, task, pane_id="w1:p1", started_at=1, conversation_id="lost-conversation")
    store.put_agent("sw", agent)
    if lane != "master":
        assert store.claim("sw", task, name, tick.LEASE_MS)
        ledger.rows[task].update(state="claimed", claimed_by=name, claim_generation=1)
    runtime.stuck.add(name)
    runtime.refusals[name] = {"process": 4242, "refusal": "pane close refused"}
    monkeypatch.setattr(tick.reaper, "scratch_homes", lambda slug, task: [f"{slug}/{task}"])
    reopen = Mock(return_value=False)
    monkeypatch.setattr(tick.dead_resume, "reopen", reopen)
    return store, ledger, runtime, agent, reopen


@pytest.mark.feature("tick-lost-agent-keeps-record-until-retired")
def test_a_lost_master_whose_resume_is_refused_keeps_its_record_while_retire_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ledger, runtime, master, reopen = _lost_agent(monkeypatch)
    at = tick.STARTUP_GRACE_MS + 2

    actions = agents._reap("sw", store, ledger, runtime, ledger.rows, at)

    assert store.agents("sw") == [master]
    assert actions == [f"could not retire {master.name}, retrying next tick"]
    assert runtime.closed == []
    assert runtime.homes == {master.name: ["sw/master"]}
    reopen.assert_called_once_with("sw", store, ledger, runtime, master, at)
    assert retire_watch.rows(store, "sw") == [
        {"agent": master.name, "ticks": 1, "since": at, "at": at, "process": 4242, "refusal": "pane close refused"}
    ]


@pytest.mark.feature("tick-lost-agent-keeps-record-until-retired")
def test_a_lost_master_is_dropped_on_the_pass_its_retire_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    store, ledger, runtime, master, reopen = _lost_agent(monkeypatch)
    at = tick.STARTUP_GRACE_MS + 2
    first = agents._reap("sw", store, ledger, runtime, ledger.rows, at)
    assert store.agents("sw") == [master]
    assert first == [f"could not retire {master.name}, retrying next tick"]
    runtime.stuck.clear()

    second = agents._reap("sw", store, ledger, runtime, ledger.rows, at + 1)

    assert second == [f"lost {master.name}"]
    assert store.agents("sw") == []
    assert runtime.closed == [master.pane_id]
    assert runtime.homes == {master.name: ["sw/master"]}
    assert retire_watch.rows(store, "sw") == []
    assert reopen.call_count == 2


@pytest.mark.feature("tick-lost-agent-keeps-record-until-retired")
def test_a_deliberate_terminate_keeps_its_record_while_retire_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    store, ledger, runtime, agent, reopen = _lost_agent(monkeypatch, lane="eng")
    deliberate_exit.record(store, "sw", agent.name, 5)
    claimed = dict(ledger.rows["t1"])
    at = tick.STARTUP_GRACE_MS + 2

    actions = agents._reap("sw", store, ledger, runtime, ledger.rows, at)

    assert store.agents("sw") == [agent]
    assert actions == [f"could not retire {agent.name}, retrying next tick"]
    assert ledger.rows["t1"] == claimed
    assert store.redis.get(store.key("sw", "claim", "t1")) == agent.name
    assert runtime.closed == []
    assert runtime.homes == {agent.name: ["sw/t1"]}
    reopen.assert_not_called()
    assert retire_watch.rows(store, "sw") == [
        {"agent": agent.name, "ticks": 1, "since": at, "at": at, "process": 4242, "refusal": "pane close refused"}
    ]
