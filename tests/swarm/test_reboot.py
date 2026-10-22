import json
from dataclasses import replace

import pytest

from hooks.context import context_recycle
from scripts.inbox.store import InboxStore
from scripts.swarm import commands, reboot, snapshot
from scripts.swarm import tick as tick_module
from scripts.swarm.store import MASTER, AgentRecord
from tests.swarm.test_dead_resume import ResumingRuntime
from tests.swarm.test_tick import tasks


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    monkeypatch.setattr(context_recycle, "_used", lambda session: 1_000)
    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, compact_limit=600))
    return result


@pytest.fixture
def swarm(store):
    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    for agent in store.agents("sw"):
        store.put_agent(
            "sw",
            replace(agent, conversation_id=f"conversation-{agent.lane}", profile="engineer", overlays=["frontend"]),
        )
    hive = AgentRecord(
        "engineer@a1b2c3-0099",
        "eng",
        "t9",
        harness="claude",
        account="hive-acct",
        conversation_id="hive-conversation",
        profile="engineer",
        runtime_target={"host_id": "anton-hive"},
    )
    store.put_agent("sw", hive)
    return ledger, runtime


def _local(store):
    return {a.name: a for a in store.agents("sw") if a.name != "engineer@a1b2c3-0099"}


def _taken(monkeypatch, written):
    def take(store, slug, now_ms, run=None):
        written.append((now_ms, reboot.record_path(slug).exists()))
        return snapshot.path(slug)

    monkeypatch.setattr(reboot.snapshot, "take", take)


def test_shutdown_records_every_local_agent_and_asks_the_master_for_its_handoff(store, swarm, monkeypatch):
    written = []
    _taken(monkeypatch, written)

    doc = reboot.prepare(store, "sw", 500)

    assert written == [(500, True)]
    assert json.loads(reboot.record_path("sw").read_text()) == doc
    assert (doc["slug"], doc["taken_at"], doc["hive"], doc["snapshot"]) == (
        "sw",
        500,
        "workstation",
        str(snapshot.path("sw")),
    )
    local = _local(store)
    assert doc["agents"] == [
        {
            "name": a.name,
            "lane": a.lane,
            "seat": a.seat,
            "task": a.task,
            "conversation_id": a.conversation_id,
            "profile": "engineer",
            "overlays": ["frontend"],
            "harness": a.harness,
            "model": a.model,
            "effort": a.effort,
            "account": a.account,
        }
        for a in store.agents("sw")
        if a.name in local
    ]
    master = next(a for a in local.values() if a.lane == MASTER)
    items = InboxStore(store.redis).inbox(master.name)
    assert [i.text for i in items] == [reboot.ASK.format(path=reboot.handoff_path("sw"))]
    assert str(reboot.handoff_path("sw")).endswith("/sw/master-shutdown-handoff.md")


def _recorded(store, monkeypatch):
    _taken(monkeypatch, [])
    reboot.prepare(store, "sw", 500)


def _booted(store, runtime, current="boot-2", quota=lambda harness, account: True):
    started = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reboot, "last_quota", quota)
        patch.setattr(reboot, "start_server", lambda: started.append(1) or [])
        actions = reboot.boot_pass("sw", store, runtime.ledger, runtime, 900, current=current)
    return actions, started


@pytest.fixture
def rebooted(store, swarm, monkeypatch):
    ledger, runtime = swarm
    _recorded(store, monkeypatch)
    store.redis.set(store.key("sw", "boot", "workstation"), "boot-1")
    runtime.live.clear()
    runtime.ledger = ledger
    return ledger, runtime


def test_a_new_boot_resumes_each_local_agent_in_its_own_conversation_seat_and_account(store, rebooted):
    ledger, runtime = rebooted
    before = _local(store)

    actions, started = _booted(store, runtime)

    assert started == [1]
    assert sorted(a.name for a in runtime.resumed) == sorted(before)
    assert "engineer@a1b2c3-0099" not in {a.name for a in runtime.resumed}
    assert sorted(actions) == sorted(f"resumed {name} after the workstation rebooted" for name in before)
    for name, agent in _local(store).items():
        assert (agent.conversation_id, agent.seat, agent.task, agent.account) == (
            before[name].conversation_id,
            before[name].seat,
            before[name].task,
            before[name].account,
        )
        assert agent.pane_id == "reopened:pane"
    assert store.redis.get(store.key("sw", "boot", "workstation")) == "boot-2"


@pytest.mark.parametrize("reading", [False, None])
def test_an_account_without_room_resumes_on_whatever_account_the_router_picks(store, rebooted, reading):
    ledger, runtime = rebooted
    local = _local(store)
    for agent in local.values():
        store.put_agent("sw", replace(agent, account="spent" if agent.lane == MASTER else "roomy"))

    _booted(store, runtime, quota=lambda harness, account: True if account == "roomy" else reading)

    routed = {a.lane: a.account for a in runtime.resumed}
    assert routed == {MASTER: "", "eng": "roomy"}


def test_the_same_boot_and_a_first_sighting_resume_nothing(store, rebooted):
    ledger, runtime = rebooted

    assert _booted(store, runtime, current="boot-1") == ([], [])
    store.redis.delete(store.key("sw", "boot", "workstation"))
    assert _booted(store, runtime, current="boot-3") == ([], [])
    assert store.redis.get(store.key("sw", "boot", "workstation")) == "boot-3"
    assert runtime.resumed == []


def test_a_live_agent_and_a_stopped_swarm_are_left_alone(store, rebooted):
    ledger, runtime = rebooted
    master = next(a for a in _local(store).values() if a.lane == MASTER)
    runtime.live.add(master.name)

    _booted(store, runtime)
    assert master.name not in {a.name for a in runtime.resumed}

    store.redis.set(store.key("sw", "boot", "workstation"), "boot-1")
    store.update("sw", state="stopped")
    runtime.resumed.clear()
    assert _booted(store, runtime) == ([], [])
    assert runtime.resumed == []
    assert store.redis.get(store.key("sw", "boot", "workstation")) == "boot-1"


def test_a_herdr_server_that_will_not_start_leaves_the_boot_for_the_next_tick(store, rebooted, monkeypatch):
    ledger, runtime = rebooted
    monkeypatch.setattr(reboot, "start_server", lambda: ["herdr server did not start after the reboot: down"])

    actions = reboot.boot_pass("sw", store, ledger, runtime, 900, current="boot-2")

    assert actions == ["herdr server did not start after the reboot: down"]
    assert runtime.resumed == []
    assert store.redis.get(store.key("sw", "boot", "workstation")) == "boot-1"


def test_an_automatic_snapshot_newer_than_the_planned_record_names_the_agents(store, rebooted):
    ledger, runtime = rebooted
    master = next(a for a in _local(store).values() if a.lane == MASTER)
    folder = snapshot.auto_dir("sw")
    folder.mkdir(parents=True, exist_ok=True)
    agents = {master.name: {}}
    (folder / "auto-700.json").write_text(
        json.dumps({"taken_at": 700, "state": {"keys": {store.key("sw", "agents"): {"value": agents}}}})
    )

    assert reboot.recorded(store, "sw") == {master.name}
    _booted(store, runtime)
    assert [a.name for a in runtime.resumed] == [master.name]


def test_no_record_and_no_snapshot_name_no_agent(store):
    assert reboot.recorded(store, "sw") == set()


def test_last_quota_reads_the_newest_reading_however_old(monkeypatch):
    from scripts import claude_quota_balancer as balancer

    class Reading:
        def __init__(self, account, cap):
            self.account, self.cap = account, cap

    monkeypatch.setattr(
        balancer,
        "cached_observations",
        lambda: [(3.0, Reading("a", 0)), (1.0, Reading("a", 3)), (2.0, Reading("b", 0))],
    )
    monkeypatch.setattr(balancer, "account_cap", lambda result: result.cap)

    assert reboot.last_quota("claude", "a") is False
    assert reboot.last_quota("claude", "b") is False
    assert reboot.last_quota("claude", "c") is None
    monkeypatch.setattr(balancer, "account_cap", lambda result: None)
    assert reboot.last_quota("claude", "a") is None


def test_last_quota_asks_the_codex_router_for_codex(monkeypatch):
    monkeypatch.setattr(reboot.resume, "account_quota", lambda harness, account: (harness, account))
    assert reboot.last_quota("codex", "x") == ("codex", "x")


def test_a_herdr_failure_is_reported_and_the_boot_still_resumes(monkeypatch):
    def refuse(environ):
        raise reboot.herdr_host.HerdrError("herdr is not installed")

    monkeypatch.setattr(reboot.herdr_host, "ensure_server", refuse)
    assert reboot.start_server() == ["herdr server did not start after the reboot: herdr is not installed"]
    monkeypatch.setattr(reboot.herdr_host, "ensure_server", lambda environ: True)
    assert reboot.start_server() == []


def test_boot_id_reads_the_kernel_boot_id(tmp_path, monkeypatch):
    path = tmp_path / "boot_id"
    path.write_text("abc-123\n")
    monkeypatch.setattr(reboot, "BOOT_ID", path)
    assert reboot.boot_id() == "abc-123"
