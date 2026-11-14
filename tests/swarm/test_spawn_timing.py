import json

import pytest

from scripts.swarm import input_wait, timing
from scripts.swarm import tick as tick_module
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm.tick import tick
from scripts.tick.steps import agents as agent_steps
from tests.swarm import test_tick

pytestmark = pytest.mark.unit
store = test_tick.store
FakeLedger, FakeRuntime, tasks = test_tick.FakeLedger, test_tick.FakeRuntime, test_tick.tasks


def finished(capsys):
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    return [row for row in rows if row.get("event") == "swarm_tick_step" and row["phase"] == "finished"]


def test_the_running_filter_keeps_the_lane_agents_that_still_own_their_task() -> None:
    agents = [
        AgentRecord("engineer@a1b2c3-0001", "eng", "t1"),
        AgentRecord("engineer@a1b2c3-0002", "eng", "t2", state="finished"),
        AgentRecord("engineer@a1b2c3-0003", "eng", "t3"),
        AgentRecord("ci@a1b2c3-0004", "ci", "t4"),
        AgentRecord("engineer@a1b2c3-0005", "eng", "t5"),
        AgentRecord("engineer@a1b2c3-0006", "eng", "t6"),
    ]
    rows = {
        "t1": {"id": "t1", "lane": "eng", "claimed_by": "engineer@a1b2c3-0001"},
        "t2": {"id": "t2", "lane": "eng", "claimed_by": "engineer@a1b2c3-0002"},
        "t3": {"id": "t3", "lane": "eng", "claimed_by": "engineer@a1b2c3-0009"},
        "t4": {"id": "t4", "lane": "ci", "claimed_by": "ci@a1b2c3-0004"},
        "t6": {"id": "t6", "lane": "eng", "state": "done", "claimed_by": "engineer@a1b2c3-0006"},
    }
    assert tick_module._running(agents, rows, "eng") == [rows["t1"], {"id": "t5", "lane": "eng"}]
    assert tick_module._running(agents, rows, "ci") == [rows["t4"]]


def test_a_reap_pass_times_each_live_agent_inside_the_reap_step(store, capsys):
    from scripts.hub.reconciler import legacy_agents

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    capsys.readouterr()
    with timing.tick("sw"):
        tick("sw", store, ledger, runtime, now_ms=2_000)
    rows = finished(capsys)
    reap = next(row for row in rows if row["step"] == timing.named(agent_steps._reap))
    inside = [row for row in rows if reap["started"] <= row["started"] and row is not reap]
    inside = [row for row in inside if row["started"] + row["wall_s"] <= reap["started"] + reap["wall_s"]]
    pass_wide = [timing.named(tick_module._live_names), timing.named(legacy_agents)]
    per_agent = [
        timing.named(tick_module._acknowledging),
        timing.named(tick_module._remote_gone_reason),
        timing.named(tick_module._restore_owner),
        timing.named(tick_module._observe),
        timing.named(tick_module._watch_agent),
    ]
    agent = "engineer@a1b2c3-0001"
    named = {row["step"] for row in inside if row["step"] in pass_wide + per_agent and row.get("agent") == agent}
    assert sorted(named) == sorted(per_agent)
    assert [sorted(row) for row in inside if row["step"] in pass_wide] == [sorted(reap)] * 2
    assert "agent" not in reap


def test_a_reap_pass_goes_on_to_the_next_agent_after_a_skipped_one(store, capsys, monkeypatch):
    from dataclasses import replace

    from scripts.hub import reconciler

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    live = store.agents("sw")
    skipped = replace(live[0], name="engineer@a1b2c3-0009", state="awaiting-decision")
    rows = {t["id"]: t for t in ledger.rows.values()}

    def watched(agents):
        monkeypatch.setattr(reconciler, "legacy_agents", lambda *_: agents)
        capsys.readouterr()
        with timing.tick("sw"):
            agent_steps._reap("sw", store, ledger, runtime, rows, 2_000)
        return [row.get("agent") for row in finished(capsys) if row["step"] == timing.named(tick_module._watch_agent)]

    alone = watched(live)
    assert alone != []
    assert watched([skipped, *live]) == alone


def watched_steps(capsys, agent):
    rows = [row for row in finished(capsys) if row.get("agent") == agent]
    watch = next(row for row in rows if row["step"] == timing.named(tick_module._watch_agent))
    end = watch["started"] + watch["wall_s"]
    return {row["step"] for row in rows if watch["started"] < row["started"] and row["started"] + row["wall_s"] <= end}


def test_a_waiting_watch_times_its_input_wait_mark_inside_the_watch_step(store, capsys):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = "engineer@a1b2c3-0001"
    runtime.statuses[agent] = "waiting"
    capsys.readouterr()
    with timing.tick("sw"):
        tick("sw", store, ledger, runtime, now_ms=2_000)
    inside = watched_steps(capsys, agent)
    assert timing.named(input_wait.mark) in inside
    assert timing.named(RedisStore.put_agent) in inside
    runtime.statuses[agent] = "idle"
    with timing.tick("sw"):
        tick("sw", store, ledger, runtime, now_ms=3_000)
    assert timing.named(input_wait.clear) in watched_steps(capsys, agent)


def test_a_master_holding_a_task_gets_no_stale_nudge(store, monkeypatch):
    from dataclasses import replace

    from scripts.swarm import stale_nudge
    from scripts.swarm.pane import PaneObservation
    from scripts.swarm.store import MASTER

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    master = replace(store.agents("sw")[0], lane=MASTER, idle_ticks=0)
    sent = []
    monkeypatch.setattr(stale_nudge, "nudge", lambda *args: sent.append(args) or ["nudged"])
    assert tick_module._stale_nudge("sw", store, master, PaneObservation("idle"), False, 2_000) == []
    assert sent == []


def test_an_idle_watch_times_each_read_inside_the_watch_step(store, capsys):
    from scripts.gates import log as gate_log
    from scripts.hive import scope
    from scripts.swarm import automated_gc, ledger_probe, outage, resume
    from scripts.swarm import idle as idle_state
    from scripts.swarm.naming import NameRegistry

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = "engineer@a1b2c3-0001"
    runtime.statuses[agent] = "idle"
    capsys.readouterr()
    with timing.tick("sw"):
        tick("sw", store, ledger, runtime, now_ms=2_000)
    inside = watched_steps(capsys, agent)
    parts = [
        timing.named(tick_module._observe),
        timing.named(tick_module._refusal_due),
        timing.named(store.redis.set),
        timing.named(outage.mark_pane),
        timing.named(outage.held),
        timing.named(resume.recovery_exhausted),
        timing.named(resume.recovery_pending),
        timing.named(tick_module._stuck_on_provider),
        timing.named(outage.open_records),
        timing.named(idle_state.heartbeat),
        timing.named(idle_state.wait),
        timing.named(NameRegistry.resolve),
        timing.named(scope.readings),
        timing.named(tick_module._operator_at_pane),
        timing.named(ledger_probe.holding),
        timing.named(automated_gc.owned),
        timing.named(RedisStore.put_agent),
        timing.named(gate_log.append),
        timing.named(resume.recovery_holds),
    ]
    assert [part for part in parts if part not in inside] == []
