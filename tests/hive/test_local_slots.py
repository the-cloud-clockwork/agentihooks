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
