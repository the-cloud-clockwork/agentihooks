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


class Store:
    redis = object()

    def __init__(self, swarms):
        self.swarms = swarms

    def slugs(self):
        return list(self.swarms)

    def config(self, slug):
        return SimpleNamespace(state=self.swarms[slug][0])

    def agents(self, slug):
        return [SimpleNamespace(harness=harness) for harness in self.swarms[slug][1]]


def hive_port(hives, readings):
    registry = SimpleNamespace(hives=lambda redis: hives, live=lambda hive, now: hive["live"])
    headroom = SimpleNamespace(read=lambda redis, hive, now: readings.get(hive))
    return SimpleNamespace(registry=registry, headroom=headroom)


def test_a_bare_host_counts_its_load_and_free_memory():
    assert monitor.capacity(HOST) == (6.0, 16, 16000, 36000)


def test_a_pod_is_held_to_its_own_cpu_and_memory_limits():
    assert monitor.capacity(POD) == (1.5, 4.0, 8000, 12000)


def test_load_over_the_core_count_reads_as_every_core_busy():
    assert monitor.capacity({**HOST, "load5": 40.0})[0] == 16


def test_view_counts_agents_by_harness_swarms_running_and_live_hives_only():
    store = Store(
        {
            "a": ("running", ["claude", "claude", "codex"]),
            "b": ("stopped", ["codex", ""]),
            "c": ("running", []),
        }
    )
    hives = [{"id": "root", "live": True}, {"id": "pod", "live": True}, {"id": "gone", "live": False}]
    port = hive_port(hives, {"root": HOST, "pod": POD, "gone": HOST})
    with mock.patch.object(monitor.ports, "hive", return_value=port):
        data = monitor.view(store, NOW)
    assert data == {
        "agents": {"total": 5, "claude": 2, "codex": 2},
        "swarms": {"running": 2},
        "hives": {"live": 2, "measured": 2},
        "cpu": {"used": 7.5, "cores": 20.0},
        "memory": {"used_mb": 24000, "total_mb": 48000},
    }


def test_a_live_hive_without_a_reading_is_counted_but_adds_no_load():
    port = hive_port([{"id": "root", "live": True}], {})
    with mock.patch.object(monitor.ports, "hive", return_value=port):
        data = monitor.view(Store({}), NOW)
    assert data["hives"] == {"live": 1, "measured": 0}
    assert data["cpu"] == {"used": 0, "cores": 0}
    assert data["memory"] == {"used_mb": 0, "total_mb": 0}


def test_an_unreachable_store_answers_503():
    from redis import RedisError

    services = SimpleNamespace(swarm=SimpleNamespace(store=mock.Mock(side_effect=RedisError("down"))))
    with pytest.raises(APIError) as caught:
        monitor.route(services)
    assert caught.value.status == 503
