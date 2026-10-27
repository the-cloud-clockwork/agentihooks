import json
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts.swarm_ledger.api import monitor
from scripts.swarm_ledger.api.errors import APIError

NOW = 1_000_000
HOST = {"cpus": 16, "load5": 6.0, "available_mb": 20000, "total_mb": 36000, "pending": 0, "measured_at": NOW}
POD = {
    "cpus": 8,
    "load5": 7.5,
    "available_mb": 9000,
    "total_mb": 16000,
    "pod_total_mb": 12000,
    "pod_available_mb": 4000,
    "pod_cpus": 4.0,
    "pod_load": 1.5,
    "pending": 0,
    "measured_at": NOW,
}


class Redis:
    def __init__(self, sessions):
        self.sessions = sessions

    def get(self, key):
        hive = key.removeprefix("hive:").removesuffix(":sessions")
        return json.dumps(self.sessions[hive]) if hive in self.sessions else None


class Store:
    def __init__(self, swarms, sessions=None):
        self.swarms = swarms
        self.redis = Redis(sessions or {})

    def slugs(self):
        return list(self.swarms)

    def config(self, slug):
        return SimpleNamespace(state=self.swarms[slug])


def ports_for(hives, readings, here="", local=None, published=None):
    registry = SimpleNamespace(
        hives=lambda redis: hives, live=lambda hive, now: hive["live"], key=lambda hive: f"hive:{hive}"
    )
    headroom = SimpleNamespace(read=lambda redis, hive, now: readings.get(hive))
    herdr_stats = SimpleNamespace(read=lambda redis, now: published)
    hooks = SimpleNamespace(account_sessions=SimpleNamespace(local_processes=lambda: local or {}))
    swarm = SimpleNamespace(commands=SimpleNamespace(hive_id=lambda: here))
    return (
        mock.patch.object(
            monitor.ports,
            "hive",
            return_value=SimpleNamespace(registry=registry, headroom=headroom, herdr_stats=herdr_stats),
        ),
        mock.patch.object(monitor.ports, "hooks", return_value=hooks),
        mock.patch.object(monitor.ports, "swarm", return_value=swarm),
    )


def viewed(store, *patches):
    with patches[0], patches[1], patches[2]:
        return monitor.view(store, NOW)


def test_a_bare_host_counts_its_load_and_free_memory():
    assert monitor.capacity(HOST) == (6.0, 16, 16000, 36000)


def test_a_pod_is_held_to_its_own_cpu_and_memory_limits():
    assert monitor.capacity(POD) == (1.5, 4.0, 8000, 12000)


def test_load_over_the_core_count_reads_as_every_core_busy():
    assert monitor.capacity({**HOST, "load5": 40.0})[0] == 16


def test_agents_come_from_this_hosts_process_table_and_each_other_hives_headroom_reading():
    store = Store({"a": "running", "b": "stopped", "c": "running"})
    hives = [{"id": "root", "live": True}, {"id": "pod", "live": True}, {"id": "gone", "live": False}]
    readings = {
        "root": {**HOST, "agents": {"claude": 99, "codex": 99}},
        "pod": {**POD, "agents": {"claude": 4, "codex": 1}},
        "gone": {**HOST, "agents": {"claude": 7}},
    }
    local = {"claude": {1: "x", 2: "y", 3: "z"}, "codex": {4: "w"}}
    data = viewed(store, *ports_for(hives, readings, here="root", local=local))
    assert data == {
        "agents": {"total": 9, "source": "hives", "reporting": 2, "of": 2, "claude": 7, "codex": 2},
        "swarms": {"running": 2},
        "hives": {"live": 2, "measured": 2},
        "cpu": {"used": 7.5, "cores": 20.0},
        "memory": {"used_mb": 24000, "total_mb": 48000},
    }


def test_a_hive_without_a_fresh_reading_is_left_out_never_counted_as_zero():
    hives = [{"id": "root", "live": True}, {"id": "pod", "live": True}]
    readings = {"root": {**HOST, "agents": {"claude": 2, "codex": 0}}}
    data = viewed(Store({}), *ports_for(hives, readings))
    assert data["agents"] == {"total": 2, "source": "hives", "reporting": 1, "of": 2, "claude": 2, "codex": 0}
    assert data["hives"] == {"live": 2, "measured": 1}


def test_a_hive_that_predates_the_agent_reading_falls_back_to_its_session_report():
    sessions = {"pod": {"claude:a": 3, "codex:b": 1, "interactive:claude:a": 1, "_executions": {"codex:b": ["x"]}}}
    data = viewed(Store({}, sessions), *ports_for([{"id": "pod", "live": True}], {"pod": HOST}))
    assert data["agents"] == {"total": 4, "source": "hives", "reporting": 1, "of": 1, "claude": 3, "codex": 1}
    assert data["hives"] == {"live": 1, "measured": 1}


def test_an_unreachable_store_answers_503():
    from redis import RedisError

    services = SimpleNamespace(swarm=SimpleNamespace(store=mock.Mock(side_effect=RedisError("down"))))
    with pytest.raises(APIError) as caught:
        monitor.route(services)
    assert caught.value.status == 503


def test_agents_are_the_panes_the_master_herdr_client_published_for_itself_and_its_machines():
    published = {
        "machines": {"master": {"claude": 11, "codex": 2}, "hive-1": {"claude": 10, "codex": 4}},
        "silent": ["hive-2"],
        "measured_at": NOW,
    }
    hives = [{"id": "root", "live": True}]
    data = viewed(Store({}), *ports_for(hives, {"root": {**HOST, "agents": {"claude": 1}}}, published=published))
    assert data["agents"] == {"total": 27, "source": "herdr", "reporting": 2, "of": 3, "claude": 21, "codex": 6}
