import json
from dataclasses import replace

import fakeredis
import pytest

from scripts.inbox import exits
from scripts.inbox.store import InboxStore
from scripts.swarm import launch_check, live_binding, master_retire
from scripts.swarm.store import MASTER, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")
WAIT = 5 * 60 * 1000
LAUNCH_CHECKED = True


@pytest.fixture
def swarm():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0))
    runtime, ledger = FakeRuntime(), FakeLedger([])
    tick("sw", store, ledger, runtime, 100)
    master = next(a for a in store.agents("sw") if a.lane == MASTER)
    master = replace(master, profile="master", model="opus", effort="high", account="team")
    store.put_agent("sw", master)
    launch_check.forget(store, "sw", master.name)
    return store, runtime, ledger, master


def mismatch(runtime, name, **wrong):
    runtime.bindings = lambda agents: {
        a.name: {**live_binding.assignment(a), "hooks": True, **(wrong if a.name == name else {})}
        for a in agents
        if a.name in runtime.live
    }


def asks(store, name):
    return [i for i in InboxStore(store.redis).inbox(name) if i.ref == master_retire.REF]


def current(store, name):
    return next(a for a in store.agents("sw") if a.name == name)


def test_a_mismatched_live_master_hands_off_before_it_is_retired(swarm):
    store, runtime, ledger, master = swarm
    inbox = InboxStore(store.redis)
    mismatch(runtime, master.name, model="sonnet")
    actions = tick("sw", store, ledger, runtime, 200)
    assert f"asked {master.name} for a handoff before retiring it: mismatched model" in actions
    assert master.name not in runtime.killed
    (ask,) = asks(store, master.name)
    assert ask.state == "pending" and "agentihooks swarm sw handoff" in ask.text and "5 minutes" in ask.text
    actions = tick("sw", store, ledger, runtime, 300)
    assert f"waiting on {master.name}'s handoff before retiring it: mismatched model" in actions
    assert master.name not in runtime.killed and len(asks(store, master.name)) == 1
    store.put_handoff("sw", MASTER, "caps stay at four")
    store.put_agent("sw", replace(current(store, master.name), state="finished"))
    exits.settle(inbox, master.name, master.seat, "handed off its seat")
    tick("sw", store, ledger, runtime, 400)
    assert runtime.killed == [master.name]
    successor, primed = runtime.masters[-1]
    assert successor != master.name
    assert primed["handoff"] == "caps stay at four"
    assert primed["launch_assignment"]["model"] == "opus"
    assert inbox.get(ask.id).state == "done"
    assert inbox.pending_items(master.seat) == [] and inbox.pending_items(successor) == []
    assert master_retire.reason(store, "sw", master.name) == ""


@pytest.mark.parametrize("minutes", ["", "2"])
def test_a_master_without_a_handoff_is_retired_at_the_deadline(swarm, monkeypatch, minutes):
    store, runtime, ledger, master = swarm
    if minutes:
        monkeypatch.setenv(master_retire.MINUTES, minutes)
    wait = int(float(minutes or 5) * 60 * 1000)
    mismatch(runtime, master.name, effort="low")
    tick("sw", store, ledger, runtime, 200)
    (ask,) = asks(store, master.name)
    assert f"{minutes or 5} minutes" in ask.text
    tick("sw", store, ledger, runtime, 200 + wait - 1)
    assert master.name not in runtime.killed
    actions = tick("sw", store, ledger, runtime, 200 + wait)
    assert any(a.startswith(f"retired {master.name} after mismatched effort") for a in actions)
    assert runtime.killed == [master.name]
    assert InboxStore(store.redis).get(ask.id).state == "done"
    assert master_retire.reason(store, "sw", master.name) == ""
    assert runtime.masters[-1][0] != master.name


def test_a_master_whose_process_is_gone_is_retired_without_asking(swarm):
    store, runtime, ledger, master = swarm
    runtime.bindings = lambda agents: {a.name: {"process": False} for a in agents}
    tick("sw", store, ledger, runtime, 200)
    assert master.name in runtime.killed
    assert asks(store, master.name) == []


def test_a_master_record_with_an_empty_field_is_flagged_and_never_retired(swarm):
    store, runtime, ledger, master = swarm
    store.put_agent("sw", replace(master, account=""))
    mismatch(runtime, master.name, account="team")
    for at in (200, 200 + WAIT, 200 + 2 * WAIT):
        tick("sw", store, ledger, runtime, at)
    assert master.name not in runtime.killed
    assert asks(store, master.name) == []
    report = json.loads(store.redis.hget(store.key("sw", "live-bindings"), master.name))
    assert (report["state"], report["differences"], report["unknown"]) == ("matching", {}, ["account"])
    assert [f.id for f in live_binding.findings(store, "sw")] == [f"live-binding/{master.name}/account"]


def test_a_stopping_swarm_asks_its_master_for_a_handoff_before_retiring_it(swarm):
    store, runtime, ledger, master = swarm
    store.update("sw", state="stopping")
    actions = tick("sw", store, ledger, runtime, 200)
    assert f"asked {master.name} for a handoff before retiring it: the swarm is stopping" in actions
    assert master.name not in runtime.killed
    store.put_handoff("sw", MASTER, "resume the review")
    store.put_agent("sw", replace(current(store, master.name), state="finished"))
    actions = tick("sw", store, ledger, runtime, 300)
    assert runtime.killed == [master.name] and actions[-1] == "stopped"
    assert store.handoff("sw", MASTER) == "resume the review"


def test_a_master_that_never_reported_is_retired_without_asking(swarm):
    store, runtime, ledger, master = swarm
    agent = replace(master, state="starting")
    assert master_retire.hold(store, "sw", agent, "test", True, 200) == ""
    assert asks(store, master.name) == []


def test_a_worker_is_never_held(swarm):
    store, _, _, master = swarm
    worker = replace(master, name="engineer@zz", lane="eng", task="t1")
    assert master_retire.hold(store, "sw", worker, "test", True, 200) == ""
    assert asks(store, worker.name) == []


def test_a_launch_check_failure_asks_a_reported_master_for_a_handoff_first(swarm, monkeypatch):
    from hooks.context import profile_chain

    monkeypatch.setattr(profile_chain, "read_state", lambda: {})
    store, runtime, ledger, master = swarm
    launch_check.begin(store, "sw", master, 100)
    actions = tick("sw", store, ledger, runtime, 100 + launch_check.DEADLINE_MS)
    asked = f"asked {master.name} for a handoff before retiring it: its launch check failed on joined"
    assert any(a.startswith(asked) for a in actions), actions
    assert master.name not in runtime.killed and len(runtime.masters) == 1
    actions = tick("sw", store, ledger, runtime, 100 + launch_check.DEADLINE_MS + WAIT)
    assert any(a.startswith(f"retired {master.name} after its launch check failed") for a in actions)
    assert runtime.killed == [master.name]


def test_an_idle_master_that_hands_off_is_retired_and_not_respawned(swarm):
    store, runtime, ledger, master = swarm
    hours = 6 * 60 * 60 * 1000
    runtime.statuses[master.name] = "idle"
    actions = tick("sw", store, ledger, runtime, hours + 200)
    assert f"asked {master.name} for a handoff before retiring it: the swarm idle limit" in actions
    runtime.statuses[master.name] = "working"
    tick("sw", store, ledger, runtime, hours + 300)
    assert master.name not in runtime.killed
    assert master_retire.reason(store, "sw", master.name) == "the swarm idle limit"
    store.put_handoff("sw", MASTER, "nothing left to do")
    store.put_agent("sw", replace(current(store, master.name), state="finished"))
    exits.settle(InboxStore(store.redis), master.name, master.seat, "handed off its seat")
    tick("sw", store, ledger, runtime, hours + 400)
    assert runtime.killed == [master.name]
    assert len(runtime.masters) == 1 and store.agents("sw") == []
    assert store.handoff("sw", MASTER) == "nothing left to do"


def test_an_idle_master_without_a_handoff_retires_at_the_deadline(swarm):
    store, runtime, ledger, master = swarm
    hours = 6 * 60 * 60 * 1000
    runtime.statuses[master.name] = "idle"
    tick("sw", store, ledger, runtime, hours + 200)
    tick("sw", store, ledger, runtime, hours + 200 + WAIT - 1)
    assert master.name not in runtime.killed
    actions = tick("sw", store, ledger, runtime, hours + 200 + WAIT)
    assert f"retired {master.name} after the swarm idle limit" in actions
    assert len(runtime.masters) == 1 and store.agents("sw") == []


def test_a_zero_wait_retires_without_asking(swarm, monkeypatch):
    store, _, _, master = swarm
    monkeypatch.setenv(master_retire.MINUTES, "0")
    assert master_retire.hold(store, "sw", master, "test", True, 200) == ""
    assert asks(store, master.name) == []


def test_a_worker_record_with_an_empty_field_is_flagged_and_never_retired():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    runtime, ledger = FakeRuntime(), FakeLedger([{"id": "one"}])
    tick("sw", store, ledger, runtime, 100)
    worker = next(a for a in store.agents("sw") if a.lane == "eng")
    store.put_agent("sw", replace(worker, profile="engineer", account=""))
    launch_check.forget(store, "sw", worker.name)
    mismatch(runtime, worker.name, account="team")
    tick("sw", store, ledger, runtime, 200)
    assert worker.name not in runtime.killed
    report = json.loads(store.redis.hget(store.key("sw", "live-bindings"), worker.name))
    assert (report["differences"], report["unknown"]) == ({}, ["account"])


def test_unknown_names_each_field_the_record_never_held():
    from scripts.swarm.store import AgentRecord

    bare = AgentRecord("master@zz", MASTER, MASTER)
    assert live_binding.unknown(bare) == ["harness", "home", "profile", "model", "effort", "account"]
    held = replace(bare, harness="claude", profile="master", model="opus", effort="high", account="team")
    assert live_binding.unknown(held) == []
    validated = replace(bare, profile_decision={"validation": {"home": "/h", "model": "opus", "effort": "high"}})
    assert live_binding.unknown(validated) == ["harness", "profile", "account"]
    assert live_binding.unknown(replace(held, harness="")) == ["harness", "home"]
    assert live_binding.unknown(replace(held, profile="")) == ["home", "profile"]
    assert live_binding.compare(bare, {"harness": "codex", "model": "x", "hooks": True}) == {}
