import pytest

from scripts.handoff import transfers
from scripts.inbox.store import InboxStore
from scripts.swarm import idle, master_retire, master_stall
from scripts.swarm.health import activity
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit
MINUTE = 60_000
START = 1_800_000_000_000


@pytest.fixture(autouse=True)
def activity_root(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "default_root", lambda: tmp_path / "activity")
    monkeypatch.setenv(master_retire.MINUTES, "5")
    monkeypatch.delenv("AGENTIHOOKS_HEALTH_STALLED_MINUTES", raising=False)


def master(store, slug="sw", pane="working", called=True):
    name = store.next_name(slug, MASTER)
    agent = AgentRecord(name, MASTER, MASTER, pane_id="w1:p1", started_at=START, seat=f"master@{slug}")
    store.put_agent(slug, agent)
    store.seats.occupy(agent.seat, agent.name, START)
    store.redis.set(store.key(slug, "pane-state", agent.name), pane)
    if called:
        activity.record(
            "Bash", {"command": "ls"}, {"AGENTIHOOKS_SWARM": slug, "AGENTIHOOKS_AGENT_NAME": name}, None, START
        )
    runtime = FakeRuntime()
    runtime.live.add(name)
    runtime.statuses[name] = "working"
    return agent, runtime


def run(store, runtime, at, slug="sw", ledger=None):
    return master_stall.run(slug, store.config(slug), store, ledger or FakeLedger([]), runtime, at)


def test_stalled_master_is_asked_for_a_handoff_then_recycled_in_its_seat(store):
    agent, runtime = master(store)
    asked = run(store, runtime, START + 10 * MINUTE)
    assert asked == [f"asked {agent.name} for a handoff before retiring it: busy with no tool call for 10 minutes"]
    items = InboxStore(store.redis).pending_items(agent.name)
    assert [i.ref for i in items] == [master_retire.REF]
    assert run(store, runtime, START + 14 * MINUTE) == [
        f"waiting on {agent.name}'s handoff before retiring it: busy with no tool call for 10 minutes"
    ]
    assert store.agents("sw")[0].state != "finished"
    recycled = run(store, runtime, START + 15 * MINUTE)
    assert recycled == [f"recycled master {agent.name} in its seat after 15 minutes busy with no tool call"]
    assert [a.state for a in store.agents("sw")] == ["finished"]
    handoff = store.handoff("sw", MASTER)
    assert "busy with no tool call for 15 minutes" in handoff and handoff.endswith("<!-- handoff complete -->\n")
    assert store.handoff_seat("sw", MASTER) == agent.seat
    [row] = transfers.list_transfers(store, "sw")
    assert (row["reason"], row["predecessor"], row["seat"]) == (master_stall.STALLED, agent.name, agent.seat)
    assert runtime.killed == []
    assert run(store, runtime, START + 16 * MINUTE) == []


@pytest.mark.parametrize("minutes", [0, 9])
def test_master_under_the_threshold_stays(store, minutes):
    _, runtime = master(store)
    assert run(store, runtime, START + minutes * MINUTE) == []
    assert InboxStore(store.redis).pending_items(store.agents("sw")[0].name) == []


def test_threshold_never_falls_under_a_foreground_tool_call(store, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HEALTH_STALLED_MINUTES", "3")
    _, runtime = master(store)
    assert run(store, runtime, START + 9 * MINUTE) == []
    assert run(store, runtime, START + 10 * MINUTE) != []


def test_threshold_follows_a_longer_stall_limit(store, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HEALTH_STALLED_MINUTES", "30")
    _, runtime = master(store)
    assert run(store, runtime, START + 29 * MINUTE) == []
    assert run(store, runtime, START + 30 * MINUTE) != []


@pytest.mark.parametrize("pane", ["idle", "waiting"])
def test_master_whose_pane_is_not_working_stays(store, pane):
    _, runtime = master(store, pane=pane)
    assert run(store, runtime, START + 60 * MINUTE) == []


def test_master_with_a_declared_wait_stays(store):
    agent, runtime = master(store)
    at = START + 60 * MINUTE
    idle.declare_wait(store.redis, "sw", agent.name, at + 30 * MINUTE, "checks", at - MINUTE)
    assert run(store, runtime, at) == []


def test_master_without_any_tool_call_record_stays(store):
    _, runtime = master(store, called=False)
    assert run(store, runtime, START + 60 * MINUTE) == []


def test_tool_call_after_the_ask_withdraws_it(store):
    agent, runtime = master(store)
    run(store, runtime, START + 10 * MINUTE)
    activity.record(
        "Bash",
        {"command": "ls"},
        {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": agent.name},
        None,
        START + 12 * MINUTE,
    )
    assert run(store, runtime, START + 16 * MINUTE) == []
    assert master_retire.reason(store, "sw", agent.name) == ""
    assert InboxStore(store.redis).pending_items(agent.name) == []
    assert store.agents("sw")[0].state != "finished"


def test_other_retire_reasons_are_left_alone(store):
    agent, runtime = master(store, pane="idle")
    master_retire.hold(store, "sw", agent, "the swarm is stopping", True, START)
    assert run(store, runtime, START + MINUTE) == []
    assert master_retire.reason(store, "sw", agent.name) == "the swarm is stopping"


@pytest.mark.parametrize("state", ["stopping", "stopped"])
def test_stopping_swarm_is_left_to_its_stop(store, state):
    _, runtime = master(store)
    store.update("sw", state=state)
    assert run(store, runtime, START + 60 * MINUTE) == []


def proof(store, parent_state):
    slug = "proof-01df01-t1199-1"
    store.create(SwarmConfig(slug, "/repo", max_eng=2, max_ci=1))
    store.redis.hset(store.key(slug, "proof-creator"), mapping={"parent": "sw", "owner": "engineer@01df01-0001"})
    return slug, FakeLedger([{"id": "t1199", "state": parent_state}, {"id": "t1", "state": "blocked"}])


def test_proof_swarm_with_a_blocked_parent_task_stops_itself(store):
    slug, ledger = proof(store, "blocked")
    agent, runtime = master(store, slug)
    assert run(store, runtime, START + 10 * MINUTE, slug, ledger) == [
        f"stopped proof swarm {slug}: its parent task is blocked and its master {agent.name} stalled"
    ]
    assert store.config(slug).state == "stopping"
    assert store.agents(slug)[0].state != "finished"


@pytest.mark.parametrize("parent_state", ["claimed", "pr"])
def test_proof_swarm_with_a_working_parent_task_recycles_its_master(store, parent_state):
    slug, ledger = proof(store, parent_state)
    agent, runtime = master(store, slug)
    assert run(store, runtime, START + 10 * MINUTE, slug, ledger)[0].startswith(f"asked {agent.name}")
    assert store.config(slug).state != "stopping"


def test_proof_swarm_without_a_recorded_parent_recycles_its_master(store):
    slug = "proof-01df01-t1199-2"
    store.create(SwarmConfig(slug, "/repo", max_eng=2, max_ci=1))
    agent, runtime = master(store, slug)
    ledger = FakeLedger([{"id": "t1199", "state": "blocked"}])
    assert run(store, runtime, START + 10 * MINUTE, slug, ledger)[0].startswith(f"asked {agent.name}")


def test_tick_recycles_a_stalled_master(store):
    agent, runtime = master(store)
    ledger = FakeLedger([])
    first = tick("sw", store, ledger, runtime, START + 10 * MINUTE)
    assert any(a.startswith(f"asked {agent.name} for a handoff") for a in first)
    later = tick("sw", store, ledger, runtime, START + 15 * MINUTE)
    assert f"recycled master {agent.name} in its seat after 15 minutes busy with no tool call" in later
    assert [row["reason"] for row in transfers.list_transfers(store, "sw")] == [master_stall.STALLED]
    tick("sw", store, ledger, runtime, START + 16 * MINUTE)
    assert agent.name in runtime.killed
