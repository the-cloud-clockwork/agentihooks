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


def call(slug, name, at, done=True):
    environ = {"AGENTIHOOKS_SWARM": slug, "AGENTIHOOKS_AGENT_NAME": name}
    activity.record("Bash", {"command": "ls"}, environ, None, at)
    if done:
        activity.finish(environ, None, at, call_id="")


def master(store, slug="sw", pane="working", called=True):
    name = store.next_name(slug, MASTER)
    agent = AgentRecord(name, MASTER, MASTER, pane_id="w1:p1", started_at=START, seat=f"master@{slug}")
    store.put_agent(slug, agent)
    store.seats.occupy(agent.seat, agent.name, START)
    store.redis.set(store.key(slug, "pane-state", agent.name), pane)
    if called:
        call(slug, name, START)
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
    found = store.handoff_envelope("sw", MASTER)
    assert (found["agent"], found["seat"], found["reason"], found["ledger"]) == (
        agent.name,
        agent.seat,
        "recycle",
        "sw",
    )
    [row] = transfers.list_transfers(store, "sw")
    assert (row["reason"], row["predecessor"], row["seat"]) == (master_stall.STALLED, agent.name, agent.seat)
    assert runtime.killed == []
    assert run(store, runtime, START + 16 * MINUTE) == []


@pytest.mark.parametrize("minutes", [0, 9])
def test_master_under_the_threshold_stays(store, minutes):
    _, runtime = master(store)
    assert run(store, runtime, START + minutes * MINUTE) == []
    assert InboxStore(store.redis).pending_items(store.agents("sw")[0].name) == []


def test_threshold_follows_a_shorter_stall_limit(store, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HEALTH_STALLED_MINUTES", "8")
    _, runtime = master(store)
    assert run(store, runtime, START + 7 * MINUTE + MINUTE // 2) == []
    assert run(store, runtime, START + 8 * MINUTE)[0].startswith("asked")


def test_starting_master_stays_inside_the_startup_grace(store, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HEALTH_STALLED_MINUTES", "3")
    _, runtime = master(store)
    assert run(store, runtime, START + 6 * MINUTE) == []
    assert run(store, runtime, START + 6 * MINUTE + 1)[0].startswith("asked")


def test_master_inside_a_tool_call_is_never_recycled(store):
    agent, runtime = master(store)
    call("sw", agent.name, START + MINUTE, done=False)
    for minutes in (10, 15, 60, 600):
        assert run(store, runtime, START + minutes * MINUTE) == []
    assert store.agents("sw")[0].state != "finished"
    assert InboxStore(store.redis).pending_items(agent.name) == []


def test_master_inside_an_agent_call_whose_subagent_called_tools_is_never_recycled(store):
    agent, runtime = master(store)
    environ = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": agent.name}
    activity.record("Agent", {}, environ, None, START + MINUTE, call_id="toolu_outer")
    activity.record("Bash", {"command": "ls"}, environ, None, START + 2 * MINUTE, call_id="toolu_inner")
    activity.finish(environ, None, START + 3 * MINUTE, call_id="toolu_inner")
    assert activity.open_calls("sw", agent.name) == ["toolu_outer"]
    for minutes in (13, 18, 60):
        assert run(store, runtime, START + minutes * MINUTE) == []
    activity.finish(environ, None, START + 61 * MINUTE, call_id="toolu_outer")
    assert activity.open_calls("sw", agent.name) == []
    assert run(store, runtime, START + 71 * MINUTE)[0].startswith(f"asked {agent.name}")


def test_unsafe_call_ids_leave_no_open_call(tmp_path):
    environ = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "master@sw-1"}
    activity.record("Bash", {}, environ, tmp_path, 1, call_id="../escape")
    activity.record("Bash", {}, environ, tmp_path, 2, call_id="")
    assert activity.open_calls("sw", "master@sw-1", tmp_path) == []
    activity.record("Bash", {}, environ, tmp_path, 3, call_id="toolu_a")
    activity.finish(environ, tmp_path, 4, call_id="../toolu_a")
    assert activity.open_calls("sw", "master@sw-1", tmp_path) == ["toolu_a"]
    activity.finish(environ, tmp_path, 5, call_id="toolu_a")
    assert activity.open_calls("sw", "master@sw-1", tmp_path) == []


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


@pytest.mark.parametrize("done", [False, True])
def test_tool_call_after_the_ask_withdraws_it(store, done):
    agent, runtime = master(store)
    run(store, runtime, START + 10 * MINUTE)
    [ask] = InboxStore(store.redis).pending_items(agent.name)
    call("sw", agent.name, START + 12 * MINUTE, done=done)
    assert run(store, runtime, START + 16 * MINUTE) == []
    assert master_retire.reason(store, "sw", agent.name) == ""
    assert InboxStore(store.redis).get(ask.id).reason == f"done: {master_stall.RESUMED}"
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


class ParentLedger(FakeLedger):
    def tasks(self, slug):
        return super().tasks(slug) if slug == "sw" else []


def proof(store, parent_state, task_id="t1199"):
    slug = "proof-01df01-t1199-1"
    store.create(SwarmConfig(slug, "/repo", max_eng=2, max_ci=1))
    store.redis.hset(store.key(slug, "proof-creator"), mapping={"parent": "sw", "owner": "engineer@01df01-0001"})
    return slug, ParentLedger([{"id": task_id, "state": parent_state}, {"id": "t1", "state": "blocked"}])


@pytest.mark.parametrize("task_id", ["t1199", "T-1199"])
def test_proof_swarm_with_a_blocked_parent_task_stops_itself(store, task_id):
    slug, ledger = proof(store, "blocked", task_id)
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


def test_tool_end_is_recorded_after_every_tool_call(monkeypatch):
    from hooks import hook_manager

    ended = []
    monkeypatch.setattr(hook_manager, "_swarm_outcome", lambda payload: None)
    monkeypatch.setattr(hook_manager, "_hive_join", lambda payload, event: None)
    monkeypatch.setattr(hook_manager, "_swarm_ledger", lambda payload: None)
    monkeypatch.setattr(activity, "finish", lambda call_id: ended.append(call_id))
    hook_manager._swarm_after_tool({"tool_use_id": "toolu_x"})
    hook_manager._swarm_after_tool({"tool_use_id": None})
    assert ended == ["toolu_x", ""]


def test_failed_tool_call_closes_its_open_call(tmp_path, monkeypatch):
    from hooks import hook_manager

    monkeypatch.setattr(activity, "default_root", lambda: tmp_path)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@sw-1")
    activity.record("Bash", {"command": "false"}, None, None, 1, call_id="toolu_failed")
    assert activity.open_calls("sw", "master@sw-1") == ["toolu_failed"]
    hook_manager.EVENT_HANDLERS["PostToolUseFailure"]({"tool_use_id": "toolu_failed"})
    assert activity.open_calls("sw", "master@sw-1") == []


@pytest.mark.parametrize("outcome", ["failed", "denied"])
def test_master_stalled_after_a_failed_or_denied_call_is_recycled(store, monkeypatch, outcome):
    from types import SimpleNamespace

    from hooks import hook_manager

    agent, runtime = master(store)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", agent.name)
    monkeypatch.setattr(activity, "time", SimpleNamespace(time=lambda: (START + MINUTE) / 1000))
    payload = {"tool_name": "Bash", "tool_input": {}, "tool_use_id": f"toolu_{outcome}"}

    def refused(payload):
        activity.record("Bash", {}, call_id=payload["tool_use_id"])
        raise hook_manager.BlockAction("denied")

    monkeypatch.setattr(hook_manager, "on_pre_tool_use", refused)
    if outcome == "failed":
        activity.record("Bash", {}, call_id=payload["tool_use_id"])
        hook_manager.EVENT_HANDLERS["PostToolUseFailure"](payload)
    else:
        with pytest.raises(hook_manager.BlockAction):
            hook_manager.EVENT_HANDLERS["PreToolUse"](payload)
    assert activity.open_calls("sw", agent.name) == []
    assert run(store, runtime, START + 11 * MINUTE)[0].startswith(f"asked {agent.name}")


def test_allowed_tool_call_stays_open_after_its_pre_tool_hook(tmp_path, monkeypatch):
    from hooks import hook_manager

    monkeypatch.setattr(activity, "default_root", lambda: tmp_path)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@sw-1")
    monkeypatch.setattr(
        hook_manager, "on_pre_tool_use", lambda payload: activity.record("Bash", {}, call_id=payload["tool_use_id"])
    )
    hook_manager.EVENT_HANDLERS["PreToolUse"]({"tool_use_id": "toolu_allowed"})
    assert activity.open_calls("sw", "master@sw-1") == ["toolu_allowed"]
    assert activity.finished_events("sw") == {}


def test_turn_end_drops_calls_a_denied_call_left_open(tmp_path, monkeypatch):
    import fakeredis

    from hooks.context import swarm_heartbeat

    monkeypatch.setattr(activity, "default_root", lambda: tmp_path)
    environ = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "master@sw-1"}
    activity.record("Bash", {}, environ, None, 1, call_id="toolu_denied")
    activity.record("Bash", {}, environ, None, 2, call_id="toolu_other")
    redis = fakeredis.FakeRedis(decode_responses=True)
    swarm_heartbeat.beat("working", environ, redis, 3)
    assert activity.open_calls("sw", "master@sw-1") == ["toolu_denied", "toolu_other"]
    swarm_heartbeat.beat("idle", environ, redis, 4)
    assert activity.open_calls("sw", "master@sw-1") == []


def test_unwritable_activity_folder_never_breaks_the_hook(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    activity.finish({"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "master@sw-1"}, blocker, 1, call_id="toolu_a")
    assert activity.finished_events("sw", blocker) == {}


def test_finish_stamps_the_bound_agent_only(tmp_path):
    root = tmp_path / "new" / "root"
    bound = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "master@sw-1"}
    activity.finish(bound, root, 5, call_id="")
    activity.finish({"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "../x"}, root, 6, call_id="")
    activity.finish({"AGENTIHOOKS_SWARM": "", "AGENTIHOOKS_AGENT_NAME": "master@sw-2"}, root, 7, call_id="")
    activity.finish({"AGENTIHOOKS_SWARM": "sw"}, root, 8, call_id="")
    activity.finish({"AGENTIHOOKS_AGENT_NAME": "master@sw-3"}, root, 8, call_id="")
    activity.finish(bound, root, 9, call_id="")
    assert [path.name for path in root.iterdir()] == ["sw"]
    assert sorted(path.name for path in (root / "sw").iterdir()) == ["master@sw-1.done"]
    assert (root / "sw" / "master@sw-1.done").read_text() == "9"
    (root / "sw" / "broken.done").write_text("x")
    assert activity.finished_events("sw", root) == {"master@sw-1": 9}
    assert activity.finished_events("none", root) == {}


def test_finish_of_an_unrecorded_call_still_stamps_the_end(tmp_path):
    bound = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "master@sw-1"}
    activity.finish(bound, tmp_path, 4, call_id="toolu_never")
    assert activity.finished_events("sw", tmp_path) == {"master@sw-1": 4}


def test_turn_end_without_open_calls_and_outside_the_root_leaves_them(tmp_path):
    activity.end_turn("sw", "master@sw-1", tmp_path)
    environ = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "master@sw-1"}
    activity.record("Bash", {}, environ, tmp_path, 1, call_id="toolu_a")
    activity.end_turn("sw", "master@sw-1", tmp_path / "other")
    assert activity.open_calls("sw", "master@sw-1", tmp_path) == ["toolu_a"]
    activity.end_turn("sw", "master@sw-1", tmp_path)
    assert activity.open_calls("sw", "master@sw-1", tmp_path) == []


def test_finish_reads_the_session_environment(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@sw-1")
    before = int(time.time() * 1000)
    activity.finish(root=tmp_path, call_id="")
    assert before <= activity.finished_events("sw", tmp_path)["master@sw-1"] <= int(time.time() * 1000)
