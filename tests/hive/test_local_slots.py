from dataclasses import replace
from types import SimpleNamespace

from scripts import terminate_agent
from scripts.swarm import local_slots
from scripts.swarm.store import AgentRecord, SwarmConfig
from tests.hive.test_controller_launch import setup as setup


def session(name, target="claude", status="alive"):
    return SimpleNamespace(name=name, target=target, status=status)


def test_live_processes_from_other_swarms_and_harnesses_consume_local_slots(setup, monkeypatch):
    rows = [
        session("engineer@aaaaaa-0001"),
        session("engineer@bbbbbb-0001", "codex"),
        session("ci@bbbbbb-0002", "codex"),
        session("planner@bbbbbb-0003"),
        session("dispatcher@bbbbbb-0004"),
    ]
    monkeypatch.setattr(terminate_agent, "sessions", lambda: rows)
    assert local_slots.running(setup.store, setup.config.slug, "task") == 5
    assert local_slots.held(setup.store, "workstation", setup.config.slug, "task") == 5
    assert local_slots.claim(setup.store, "workstation", setup.config.slug, "task", "new", 5) == 5
    assert setup.store.redis.hgetall(local_slots.key("workstation")) == {}


def test_processes_and_swarm_records_count_once_and_masters_do_not_consume_slots(setup, monkeypatch):
    setup.store.create(SwarmConfig("doctor", "agentihooks", 1, 1))
    setup.store.put_agent("doctor", AgentRecord("engineer@bbbbbb-0001", "eng", "other"))
    setup.store.put_agent(setup.config.slug, AgentRecord("engineer@aaaaaa-0001", "eng", "task"))
    rows = [
        session("engineer@bbbbbb-0001", "codex"),
        session("engineer@bbbbbb-0001", "codex"),
        session("engineer@aaaaaa-0001"),
        session("master@bbbbbb-0002"),
        session(""),
    ]
    monkeypatch.setattr(terminate_agent, "sessions", lambda: rows)
    assert local_slots.running(setup.store, setup.config.slug, "task") == 1
    assert local_slots.held(setup.store, "workstation", setup.config.slug, "task") == 1


def test_an_unregistered_local_lane_process_still_consumes_a_slot(setup, monkeypatch):
    rows = [session("engineer@bbbbbb-0001", "codex", "unregistered")]
    monkeypatch.setattr(terminate_agent, "sessions", lambda: rows)
    assert local_slots.running(setup.store, setup.config.slug, "task") == 1


def test_agents_recorded_on_this_hive_consume_local_slots_but_remote_hives_do_not(setup, monkeypatch):
    from scripts.swarm import commands

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    rows = [
        AgentRecord("local", "ci", "other", hive="workstation"),
        AgentRecord("remote", "eng", "remote", hive="anton"),
        AgentRecord("ended", "eng", "old", state="ended"),
    ]
    monkeypatch.setattr(setup.store, "agents", lambda _: rows)
    assert local_slots.running(setup.store, setup.config.slug, "task") == 1


def test_a_process_absorbs_its_pending_slot_before_the_swarm_record_arrives(setup, monkeypatch):
    from scripts.swarm import lease

    monkeypatch.setattr(terminate_agent, "sessions", lambda: [session("engineer@bbbbbb-0001", "codex")])
    setup.store.redis.hset(local_slots.key("workstation"), "doctor/engineer@bbbbbb-0001", lease.now_ms(setup.store))
    assert local_slots.held(setup.store, "workstation", setup.config.slug, "task") == 1


def test_root_hive_reports_all_local_lane_processes_against_its_limit(setup, monkeypatch):
    from scripts.hive import launch
    from scripts.swarm import commands
    from tests.hive.test_controller_launch import write_hive

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [session("engineer@bbbbbb-0001", "codex")])
    write_hive(setup, id="workstation", ui="yes", max_agents=1)
    root = next(hive for hive in launch.loaded_hives(setup.store) if hive["id"] == "workstation")
    assert root["agents"] == 1
    assert root["room"] == 0
    assert "max agents" in root["refusal"]


def test_whole_machine_occupancy_includes_pending_claims_without_excluding_any_task(setup, monkeypatch):
    from scripts.swarm import lease

    monkeypatch.setattr(terminate_agent, "sessions", lambda: [session("engineer@bbbbbb-0001", "codex")])
    setup.store.put_agent(setup.config.slug, AgentRecord("ended", "eng", "old", state="ended"))
    setup.store.redis.hset(local_slots.key("workstation"), "doctor/pending", lease.now_ms(setup.store))
    assert local_slots.occupancy(setup.store, "workstation") == (1, 2)


def test_one_live_worker_absorbs_its_claim_among_several_local_workers(setup, monkeypatch):
    from scripts.swarm import lease

    rows = [session("engineer@bbbbbb-0001", "codex"), session("ci@cccccc-0001")]
    monkeypatch.setattr(terminate_agent, "sessions", lambda: rows)
    setup.store.redis.hset(local_slots.key("workstation"), "doctor/engineer@bbbbbb-0001", lease.now_ms(setup.store))
    assert local_slots.held(setup.store, "workstation", setup.config.slug, "task") == 2


def test_root_hive_room_includes_pending_claims_before_any_process_exists(setup, monkeypatch):
    from scripts.hive import launch
    from scripts.swarm import commands
    from tests.hive.test_controller_launch import write_hive

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    write_hive(setup, id="workstation", ui="yes", max_agents=1)
    local_slots.claim(setup.store, "workstation", setup.config.slug, "other", "pending", 1)
    hives = {row["id"]: row for row in launch.loaded_hives(setup.store)}
    assert hives["workstation"]["agents"] == 0
    assert hives["workstation"]["room"] == 0
    assert hives["remote"]["room"] == 3


def test_a_hive_at_its_cap_launches_nothing_when_its_session_report_misses_recorded_agents(setup, monkeypatch):
    import json

    from scripts.hive import launch, placement, registry
    from scripts.swarm import commands

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    setup.store.create(SwarmConfig("doctor", "agentihooks", 1, 1))
    rows = [
        (setup.config.slug, AgentRecord("engineer@aaaaaa-0001", "eng", "pinned", hive="remote", execution_id="e1")),
        (setup.config.slug, AgentRecord("ci@aaaaaa-0002", "ci", "free", hive="remote", execution_id="e2")),
        ("doctor", AgentRecord("engineer@bbbbbb-0001", "eng", "other", hive="remote", execution_id="e3")),
        ("doctor", AgentRecord("master@bbbbbb-0002", "master", "master", hive="remote", execution_id="e4")),
        ("doctor", AgentRecord("engineer@bbbbbb-0003", "eng", "gone", hive="remote", state="ended")),
    ]
    for slug, record in rows:
        setup.store.put_agent(slug, record if record.state == "ended" else replace(record, state="working"))
    report = {"claude:account": 3, "interactive:claude:account": 3, "_executions": {"claude:account": ["e1", "e2"]}}
    setup.store.redis.set(f"{registry.key('remote')}:sessions", json.dumps(report))
    remote = next(hive for hive in launch.loaded_hives(setup.store) if hive["id"] == "remote")
    assert remote["agents"] == 3
    assert remote["room"] == 0
    assert "max agents" in remote["refusal"]
    assert isinstance(setup.launcher.select(setup.config, "eng", {"id": "task"}), placement.Wait)
