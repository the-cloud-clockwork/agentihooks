from dataclasses import replace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli
from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm.tick import tick
from tests.swarm.test_cli import env as env
from tests.swarm.test_cli import run
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit
HOUR = 60 * 60 * 1000


def idle_master(store, state):
    store.update("sw", state=state)
    name = store.next_name("sw", MASTER)
    agent = AgentRecord(name, MASTER, MASTER, pane_id="w1:p1", started_at=1, seat="master@sw")
    store.put_agent("sw", agent)
    store.seats.occupy(agent.seat, agent.name, 1)
    rt = FakeRuntime()
    rt.live.add(agent.name)
    rt.statuses[agent.name] = "idle"
    return agent, rt


def test_old_idle_master_retires_with_the_master_scratch_homes(store, scratch):
    homes = scratch(MASTER)
    agent, rt = idle_master(store, "running")
    tick("sw", store, FakeLedger([]), rt, 6 * HOUR + 2)
    assert rt.homes == {agent.name: homes}


@pytest.mark.parametrize("state", ["running", "paused", "drained", "stopped"])
def test_old_idle_master_retires_once_with_recap_and_closed_pane(store, state):
    agent, rt = idle_master(store, state)
    ledger = FakeLedger([])
    actions = tick("sw", store, ledger, rt, 6 * HOUR + 2)
    assert store.agents("sw") == []
    assert rt.killed == [agent.name] and rt.closed == [agent.pane_id]
    assert len(store.memory.recaps(agent.seat)) == 1
    assert store.memory.recaps(agent.seat)[0]["occupant"] == agent.name
    assert store.seats.exit_of(agent.name) is not None
    assert any("retired" in action for action in actions)
    tick("sw", store, ledger, rt, 7 * HOUR)
    assert store.agents("sw") == [] and rt.masters == []


@pytest.mark.parametrize("state", ["running", "paused", "drained", "stopped"])
def test_master_just_under_limit_stays(store, state):
    agent, rt = idle_master(store, state)
    tick("sw", store, FakeLedger([]), rt, 6 * HOUR)
    assert [a.name for a in store.agents("sw")] == [agent.name]
    assert rt.killed == []


def test_master_idle_limit_comes_from_environment(store, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_MASTER_IDLE_HOURS", "2")
    _, rt = idle_master(store, "paused")
    tick("sw", store, FakeLedger([]), rt, 2 * HOUR + 2)
    assert store.agents("sw") == []


@pytest.mark.parametrize("reason", ["seat inbox", "agent inbox", "recent ledger", "working", "worker"])
def test_master_with_activity_or_work_stays(store, reason):
    agent, rt = idle_master(store, "paused")
    ledger = FakeLedger([])
    if reason.endswith("inbox"):
        address = agent.seat if reason == "seat inbox" else agent.name
        InboxStore(store.redis).send("operator", address, "please answer")
    elif reason == "recent ledger":
        ledger.events = lambda slug: [{"at": 6 * HOUR, "by": "operator"}]
    elif reason == "working":
        rt.statuses[agent.name] = "working"
    else:
        worker = AgentRecord("sw-eng-1", "eng", "t1", started_at=1)
        store.put_agent("sw", worker)
        rt.live.add(worker.name)
        ledger.rows["t1"] = {"id": "t1", "state": "claimed", "claimed_by": worker.name}
    tick("sw", store, ledger, rt, 6 * HOUR + 2)
    assert agent.name in {a.name for a in store.agents("sw")}
    assert rt.killed == []


def test_master_that_cannot_retire_keeps_record_and_retries(store):
    agent, rt = idle_master(store, "paused")
    rt.stuck.add(agent.name)
    actions = tick("sw", store, FakeLedger([]), rt, 6 * HOUR + 2)
    assert store.agents("sw") == [agent]
    assert store.memory.recaps(agent.seat) == []
    assert any("could not retire" in action for action in actions)


@pytest.mark.parametrize("state", ["running", "paused", "drained", "stopped"])
@pytest.mark.parametrize("lane", ["eng", "ci"])
@pytest.mark.parametrize("ending", ["done", "blocked", "handoff"])
def test_workers_end_by_next_tick_in_every_state(store, state, lane, ending):
    store.update("sw", state=state)
    worker = AgentRecord("worker", lane, "t1", pane_id="w1:p2", started_at=1, seat=f"{lane}-1@sw")
    if ending == "handoff":
        worker = replace(worker, state="finished")
        store.put_handoff("sw", "t1", "continue this work", seat=worker.seat)
    store.put_agent("sw", worker)
    store.seats.occupy(worker.seat, worker.name, 1)
    rt = FakeRuntime()
    rt.live.add(worker.name)
    ledger = FakeLedger(
        [{"id": "t1", "state": "claimed" if ending == "handoff" else ending, "claimed_by": worker.name}]
    )
    tick("sw", store, ledger, rt, 2)
    assert worker.name not in {a.name for a in store.agents("sw")}
    assert rt.killed == [worker.name] and worker.pane_id in rt.closed
    assert ledger.rows["t1"]["state"] != "blocked" if ending == "handoff" else ledger.rows["t1"]["state"] == ending


@pytest.mark.parametrize("wake", ["inbox", "new task"])
@pytest.mark.parametrize("state", ["paused", "drained", "stopped"])
def test_retired_master_returns_for_message_or_new_task(store, state, wake):
    _, rt = idle_master(store, state)
    ledger = FakeLedger([])
    tick("sw", store, ledger, rt, 6 * HOUR + 2)
    assert store.agents("sw") == []
    if wake == "inbox":
        InboxStore(store.redis).send("operator", "master@sw", "please answer")
    else:
        ledger.rows["new"] = {"id": "new", "lane": "eng", "state": "open"}
    tick("sw", store, ledger, rt, 6 * HOUR + 3)
    assert len([a for a in store.agents("sw") if a.lane == MASTER]) == 1


@pytest.mark.parametrize("task_state", ["open", "done", "blocked"])
def test_start_after_master_retirement_runs_workers_only_with_claimable_work(env, monkeypatch, task_state):
    store, ledger, rt = env
    assert run("sw", "create", "--repo", "/repo", "--max-eng-agents", "1", "--max-ci-agents", "0") == 0
    agent, idle_rt = idle_master(store, "paused")
    rt.live = idle_rt.live
    rt.statuses = idle_rt.statuses
    ledger.rows = {"t1": {"id": "t1", "state": task_state, "lane": "eng"}}
    tick("sw", store, ledger, rt, 6 * HOUR + 2)
    assert store.agents("sw") == [] and agent.name in rt.killed
    monkeypatch.setattr(cli, "now_ms", lambda: 6 * HOUR + 3)
    assert run("sw", "start") == 0
    assert len(rt.masters) == 1
    assert [a.name for a in store.agents("sw") if a.lane == MASTER] == ["master@a1b2c3-0002"]
    assert len(rt.spawned) == (1 if task_state == "open" else 0)
    assert store.config("sw").state == ("running" if task_state == "open" else "drained")


@pytest.mark.parametrize("state", ["running", "paused", "drained", "stopped"])
def test_space_closes_when_last_master_retires(store, state):
    _, rt = idle_master(store, state)
    tick("sw", store, FakeLedger([]), rt, 6 * HOUR + 2)
    assert rt.closed_spaces == ["sw"]


def test_stopped_swarm_with_no_agents_closes_its_space(store):
    store.update("sw", state="stopped")
    rt = FakeRuntime()
    tick("sw", store, FakeLedger([]), rt, 1)
    assert rt.closed_spaces == ["sw"]


def test_space_with_a_master_stays_open(store):
    _, rt = idle_master(store, "paused")
    tick("sw", store, FakeLedger([]), rt, 1)
    assert rt.closed_spaces == []


@pytest.mark.parametrize("label", ["swarm-sw", "space"])
@pytest.mark.parametrize("occupied", [False, True])
def test_runtime_closes_only_the_empty_swarm_space(store, occupied, label):
    from scripts.swarm import naming
    from scripts.swarm.runtime import HerdrRuntime

    calls = []
    config = store.ensure_code("sw")
    label = naming.space(config.repo, config.code) if label == "space" else label

    def herdr(argv):
        calls.append(argv)
        if argv == ["workspace", "list"]:
            return {"workspaces": [{"workspace_id": "w2", "label": label}, {"workspace_id": "w3", "label": "other"}]}
        if argv == ["agent", "list"]:
            return {
                "agents": [{"workspace_id": "w2", "pane_id": "w2:p1"}]
                if occupied
                else [{"workspace_id": "w3", "pane_id": "w3:p1"}]
            }
        return {}

    rt = HerdrRuntime(herdr=herdr)
    assert rt.close_space(store.config("sw")) is (not occupied)
    assert (["workspace", "close", "w2"] in calls) is (not occupied)
    assert ["workspace", "close", "w3"] not in calls


def test_runtime_keeps_space_when_herdr_cannot_list_agents(store):
    from scripts.swarm.runtime import HerdrRuntime

    calls = []

    def herdr(argv):
        calls.append(argv)
        if argv == ["workspace", "list"]:
            return {"workspaces": [{"workspace_id": "w2", "label": "swarm-sw"}]}
        raise RuntimeError("unavailable")

    assert not HerdrRuntime(herdr=herdr).close_space(store.config("sw"))
    assert ["workspace", "close", "w2"] not in calls


def test_runtime_reports_failed_pane_close_so_next_tick_retries():
    from scripts.swarm.runtime import HerdrRuntime

    calls = []

    def herdr(argv):
        calls.append(argv)
        raise RuntimeError("pane close failed")

    agent = AgentRecord("worker", "eng", "t1", pane_id="w1:p2")
    assert not HerdrRuntime(herdr=herdr).retire(agent, False)
    assert calls == [["pane", "close", agent.pane_id]]


def test_reopen_after_idle_retirement_starts_a_fresh_master(env, monkeypatch):
    store, ledger, rt = env
    assert run("sw", "create", "--repo", "/repo", "--max-eng-agents", "0", "--max-ci-agents", "0") == 0
    agent, idle_rt = idle_master(store, "paused")
    rt.live, rt.statuses = idle_rt.live, idle_rt.statuses
    ledger.rows = {}
    ledger.reopen = lambda slug, by: None
    tick("sw", store, ledger, rt, 6 * HOUR + 2)
    assert store.agents("sw") == [] and agent.name in rt.killed
    monkeypatch.setattr(cli, "now_ms", lambda: 6 * HOUR + 3)
    assert run("sw", "reopen") == 0
    assert len(rt.masters) == 1
    assert [a.name for a in store.agents("sw") if a.lane == MASTER] == ["master@a1b2c3-0002"]
    assert rt.spawned == []


def test_runtime_accepts_pane_already_closed_by_terminate_agent():
    from scripts.swarm.runtime import HerdrRuntime

    def herdr(argv):
        raise RuntimeError("pane not found")

    agent = AgentRecord("worker", "eng", "t1", pane_id="w1:p2")
    assert HerdrRuntime(herdr=herdr).retire(agent, False)


def test_runtime_keeps_space_without_a_valid_agent_inventory(store):
    from scripts.swarm.runtime import HerdrRuntime

    calls = []

    def herdr(argv):
        calls.append(argv)
        if argv == ["workspace", "list"]:
            return {"workspaces": [{"workspace_id": "w2", "label": "swarm-sw"}]}
        return {}

    assert not HerdrRuntime(herdr=herdr).close_space(store.config("sw"))
    assert ["workspace", "close", "w2"] not in calls
