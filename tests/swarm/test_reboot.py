import json
from dataclasses import replace

import pytest

from hooks.context import context_recycle
from scripts.handoff import transfers
from scripts.inbox.store import InboxStore
from scripts.swarm import commands, reboot, resume, snapshot
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
    monkeypatch.setattr(reboot, "boot_id", lambda: "boot-1")
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
    store.put_agent("sw", replace(hive, name="master@a1b2c3-0098", lane=MASTER, task=MASTER))
    return ledger, runtime


def _local(store):
    return {
        a.name: a for a in store.agents("sw") if not a.name.startswith(("engineer@a1b2c3-0099", "master@a1b2c3-0098"))
    }


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
    assert (doc["slug"], doc["taken_at"], doc["hive"], doc["boot"], doc["snapshot"]) == (
        "sw",
        500,
        "workstation",
        "boot-1",
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
    assert InboxStore(store.redis).inbox("master@a1b2c3-0098") == []
    assert str(reboot.handoff_path("sw")).endswith("/sw/master-shutdown-handoff.md")


def _recorded(store, monkeypatch):
    _taken(monkeypatch, [])
    reboot.prepare(store, "sw", 500)


def _booted(store, ledger, runtime, current="boot-2", quota=lambda harness, account: True):
    started = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reboot, "last_quota", quota)
        patch.setattr(reboot, "start_server", lambda: started.append(1) or [])
        actions = reboot.boot_pass("sw", store, ledger, runtime, 900, current=current)
    return actions, started


@pytest.fixture
def rebooted(store, swarm, monkeypatch):
    ledger, runtime = swarm
    _recorded(store, monkeypatch)
    store.redis.set(store.key("sw", "boot", "workstation"), "boot-1")
    runtime.live.clear()
    return ledger, runtime


def test_a_new_boot_resumes_each_local_agent_in_its_own_conversation_seat_and_account(store, rebooted):
    ledger, runtime = rebooted
    before = _local(store)

    actions, started = _booted(store, ledger, runtime)

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


def test_an_agent_that_cannot_reopen_awaits_a_resume_or_fresh_decision(store, rebooted):
    ledger, runtime = rebooted
    eng = next(a for a in _local(store).values() if a.lane == "eng")
    store.put_agent("sw", replace(eng, account="spent"))
    store.put_handoff("sw", eng.task, "carry on from the parser", seat=eng.seat)

    actions, _ = _booted(store, ledger, runtime, quota=lambda harness, account: account != "spent")

    action = f"{eng.name} awaits a resume or fresh decision after the workstation rebooted"
    assert action in actions
    held = _local(store)[eng.name]
    assert (held.state, held.conversation_id, held.seat, held.account) == (
        resume.AWAITING,
        eng.conversation_id,
        eng.seat,
        "spent",
    )
    assert eng.name not in {a.name for a in runtime.resumed}
    (outcome,) = [r for r in store.restored("sw") if r["name"] == eng.name]
    assert (outcome["outcome"], outcome["task"], outcome["conversation_id"]) == (
        resume.AWAITING,
        eng.task,
        eng.conversation_id,
    )
    assert transfers.get(store, "sw", outcome["transfer"])["handoff"] == "carry on from the parser"
    assert ledger.rows[eng.task]["claimed_by"] == eng.name
    assert f"{action}; nothing was started in its place." in ledger.notes
    assert not any(note.startswith("Starting a fresh agent") for note in ledger.notes)


def test_a_resume_that_fails_to_start_awaits_a_decision_without_a_fresh_start_notice(store, rebooted):
    ledger, runtime = rebooted
    eng = next(a for a in _local(store).values() if a.lane == "eng")

    def refuse(config, agent, text):
        raise RuntimeError("herdr refused the pane")

    runtime.resume = refuse

    actions, _ = _booted(store, ledger, runtime)

    assert f"{eng.name} awaits a resume or fresh decision after the workstation rebooted" in actions
    assert _local(store)[eng.name].state == resume.AWAITING
    assert not any(note.startswith("Starting a fresh agent") for note in ledger.notes)


def test_a_master_that_cannot_reopen_carries_its_shutdown_handoff(store, rebooted):
    ledger, runtime = rebooted
    master = next(a for a in _local(store).values() if a.lane == MASTER)
    store.put_agent("sw", replace(master, conversation_id=""))
    reboot.handoff_path("sw").write_text("## Intent\nKeep the swarm running\n")

    _booted(store, ledger, runtime)

    (outcome,) = [r for r in store.restored("sw") if r["name"] == master.name]
    assert transfers.get(store, "sw", outcome["transfer"])["handoff"] == "## Intent\nKeep the swarm running\n"


def test_a_boot_with_every_recorded_agent_live_starts_nothing(store, rebooted):
    ledger, runtime = rebooted
    runtime.live.update(_local(store))

    assert _booted(store, ledger, runtime) == ([], [])
    assert store.redis.get(store.key("sw", "boot", "workstation")) == "boot-2"


@pytest.mark.parametrize("reading", [False, None])
def test_an_account_without_room_resumes_on_whatever_account_the_router_picks(store, rebooted, reading):
    ledger, runtime = rebooted
    local = _local(store)
    for agent in local.values():
        store.put_agent("sw", replace(agent, account="spent" if agent.lane == MASTER else "roomy"))

    _booted(store, ledger, runtime, quota=lambda harness, account: True if account == "roomy" else reading)

    routed = {a.lane: a.account for a in runtime.resumed}
    assert routed == {MASTER: "", "eng": "roomy"}


def test_the_same_boot_and_a_first_sighting_resume_nothing(store, rebooted):
    ledger, runtime = rebooted

    assert _booted(store, ledger, runtime, current="boot-1") == ([], [])
    store.redis.delete(store.key("sw", "boot", "workstation"))
    assert _booted(store, ledger, runtime, current="boot-3") == ([], [])
    assert store.redis.get(store.key("sw", "boot", "workstation")) == "boot-3"
    assert runtime.resumed == []


def test_a_live_agent_and_a_stopped_swarm_are_left_alone(store, rebooted):
    ledger, runtime = rebooted
    master = next(a for a in _local(store).values() if a.lane == MASTER)
    runtime.live.add(master.name)

    _booted(store, ledger, runtime)
    assert master.name not in {a.name for a in runtime.resumed}

    store.redis.set(store.key("sw", "boot", "workstation"), "boot-1")
    store.update("sw", state="stopped")
    runtime.resumed.clear()
    assert _booted(store, ledger, runtime) == ([], [])
    assert runtime.resumed == []
    assert store.redis.get(store.key("sw", "boot", "workstation")) == "boot-2"
    store.update("sw", state="running")
    assert _booted(store, ledger, runtime) == ([], [])


def test_a_planned_record_from_an_older_boot_names_no_agent(store, rebooted):
    ledger, runtime = rebooted
    store.redis.set(store.key("sw", "boot", "workstation"), "boot-0")

    assert reboot.recorded(store, "sw", "boot-0") == set()
    assert _booted(store, ledger, runtime) == ([], [])
    assert runtime.resumed == []


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

    assert reboot.recorded(store, "sw", "boot-1") == {master.name}
    _booted(store, ledger, runtime)
    assert [a.name for a in runtime.resumed] == [master.name]


def test_no_record_and_no_snapshot_name_no_agent(store):
    assert reboot.recorded(store, "sw", "boot-1") == set()


def test_last_quota_reuses_a_stale_reading_only_until_a_window_resets(monkeypatch):
    from scripts import claude_quota_balancer as balancer
    from scripts import session_bands

    class Window:
        def __init__(self, resets_at):
            self.resets_at = resets_at

    class Reading:
        def __init__(self, account, cap, resets=(500, 900)):
            self.account, self.cap = account, cap
            self.five_hour, self.seven_day = Window(resets[0]), Window(resets[1])

    readings = [
        (30.0, Reading("a", 0)),
        (10.0, Reading("a", 3)),
        (20.0, Reading("b", 2, resets=(150, 900))),
        (20.0, Reading("c", 2, resets=(None, 900))),
    ]
    monkeypatch.setattr(balancer, "cached_observations", lambda: readings)
    monkeypatch.setattr(balancer, "account_cap", lambda result, now: result.cap)
    monkeypatch.setattr(session_bands, "fresh", lambda at, now: now - at <= 50)

    assert reboot.last_quota("claude", "a", now=60) is False
    assert reboot.last_quota("claude", "a", now=200) is False
    assert reboot.last_quota("claude", "b", now=60) is True
    assert reboot.last_quota("claude", "b", now=200) is None
    assert reboot.last_quota("claude", "c", now=200) is None
    assert reboot.last_quota("claude", "a", now=600) is None
    assert reboot.last_quota("claude", "d", now=60) is None
    monkeypatch.setattr(balancer, "account_cap", lambda result, now: None)
    assert reboot.last_quota("claude", "a", now=60) is None


def test_last_quota_asks_the_codex_router_for_codex(monkeypatch):
    monkeypatch.setattr(reboot.resume, "account_quota", lambda harness, account: (harness, account))
    assert reboot.last_quota("codex", "x") == ("codex", "x")


def test_start_server_reports_a_herdr_failure_as_an_action(monkeypatch):
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
