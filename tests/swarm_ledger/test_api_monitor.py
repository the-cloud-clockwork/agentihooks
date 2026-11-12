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


def ports_for(
    hives: list[dict],
    readings: dict,
    here: str = "",
    local: dict | None = None,
    published: dict | None = None,
    reads: list | None = None,
    bridges: dict | None = None,
) -> tuple:
    registry = SimpleNamespace(
        hives=lambda redis: hives if isinstance(redis, Redis) else [],
        live=lambda hive, now: hive["live"] and now == NOW,
        key=lambda hive: f"hive:{hive}",
    )
    headroom = SimpleNamespace(
        read=lambda redis, hive, now: readings.get(hive) if isinstance(redis, Redis) and now == NOW else None
    )

    def read(redis: object, hive: str, now: int) -> dict | None:
        (reads if reads is not None else []).append((redis, hive, now))
        if not isinstance(redis, Redis) or now != NOW:
            return None
        return bridges.get(hive) if bridges is not None else (published if hive == "root" else None)

    herdr_stats = SimpleNamespace(read=read, MASTER="master")
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


def test_a_pod_without_a_load_reading_keeps_the_node_load_under_its_core_limit():
    assert monitor.capacity({**HOST, "load5": 2.0, "pod_cpus": 4.0})[:2] == (2.0, 4.0)


def test_a_pod_memory_limit_without_its_available_figure_keeps_the_node_memory():
    assert monitor.capacity({**HOST, "pod_total_mb": 1000}) == (6.0, 16, 16000, 36000)


def test_the_monitor_route_answers_get_with_the_services_it_was_given(monkeypatch):
    from scripts.swarm_ledger.api import routes

    services = object()
    monkeypatch.setattr(monitor, "route", lambda given: {"data": given})
    handler = SimpleNamespace(command="GET", path="/api/v1/monitor")
    assert routes.global_resource(handler, services, ["monitor"]) == {"data": services}


def test_more_memory_available_than_the_total_reads_as_none_in_use():
    assert monitor.capacity({**HOST, "available_mb": 40000})[2] == 0


def test_no_reading_gives_zero_cpu_and_memory():
    data = viewed(Store({}), *ports_for([], {}))
    assert (data["cpu"], data["memory"]) == ({"used": 0, "cores": 0}, {"used_mb": 0, "total_mb": 0})


def test_cpu_figures_are_rounded_to_one_decimal():
    reading = {**POD, "pod_cpus": 4.25, "pod_load": 1.234}
    data = viewed(Store({}), *ports_for([POD_HIVE], {"pod": reading}))
    assert data["cpu"] == {"used": 1.2, "cores": 4.2}


ROOT = {"id": "root", "name": "root", "ui": "yes", "live": True}
POD_HIVE = {"id": "pod", "name": "swarm-hive-001", "ui": "no", "live": True}


def test_agents_come_from_this_hosts_process_table_and_each_other_hives_headroom_reading():
    store = Store({"a": "running", "b": "stopped", "c": "running"})
    hives = [POD_HIVE, ROOT, {"id": "gone", "name": "gone", "live": False}]
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
        "hives": {
            "live": 2,
            "measured": 2,
            "rows": [{"name": "swarm-master", "agents": 4}, {"name": "swarm-hive-001", "agents": 5}],
        },
        "cpu": {"used": 7.5, "cores": 20.0},
        "memory": {"used_mb": 24000, "total_mb": 48000},
    }


def test_a_hive_without_a_fresh_reading_is_left_out_never_counted_as_zero():
    readings = {"root": {**HOST, "agents": {"claude": 2, "codex": 0}}}
    data = viewed(Store({}), *ports_for([ROOT, POD_HIVE], readings))
    assert data["agents"] == {"total": 2, "source": "hives", "reporting": 1, "of": 2, "claude": 2, "codex": 0}
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": 2}, {"name": "swarm-hive-001", "agents": None}]
    assert data["hives"]["measured"] == 1


def test_a_hive_that_predates_the_agent_reading_falls_back_to_its_session_report():
    sessions = {
        "pod": {
            "claude:a": 3,
            "claude:b": 2,
            "codex:b": 1,
            "interactive:claude:a": 1,
            "_executions": {"codex:b": ["x"]},
        }
    }
    data = viewed(Store({}, sessions), *ports_for([POD_HIVE], {"pod": HOST}))
    assert data["agents"] == {"total": 6, "source": "hives", "reporting": 1, "of": 1, "claude": 5, "codex": 1}
    assert data["hives"] == {"live": 1, "measured": 1, "rows": [{"name": "swarm-hive-001", "agents": 6}]}


def test_an_unreachable_store_answers_503():
    from redis import RedisError

    services = SimpleNamespace(swarm=SimpleNamespace(store=mock.Mock(side_effect=RedisError("down"))))
    with pytest.raises(APIError) as caught:
        monitor.route(services)
    assert (caught.value.status, caught.value.code, str(caught.value)) == (
        503,
        "storage_error",
        "The swarm store is unreachable",
    )


def test_agents_are_the_panes_the_master_herdr_client_published_for_itself_and_its_machines():
    published = {
        "machines": {"master": {"claude": 11, "codex": 2}, "hive-1": {"claude": 10, "codex": 4}},
        "silent": ["hive-2"],
        "measured_at": NOW,
    }
    data = viewed(Store({}), *ports_for([ROOT], {"root": {**HOST, "agents": {"claude": 1}}}, published=published))
    assert data["agents"] == {"total": 27, "source": "herdr", "reporting": 2, "of": 3, "claude": 21, "codex": 6}

    assert data["hives"]["rows"] == [
        {"name": "swarm-master", "agents": 13},
        {"name": "hive-1", "agents": 14},
        {"name": "hive-2", "agents": None},
    ]


def test_a_hive_reading_without_a_harness_counts_it_as_zero():
    data = viewed(Store({}), *ports_for([POD_HIVE], {"pod": {**POD, "agents": {"claude": 4}}}))
    assert data["agents"] == {"total": 4, "source": "hives", "reporting": 1, "of": 1, "claude": 4, "codex": 0}
    assert data["hives"]["rows"] == [{"name": "swarm-hive-001", "agents": 4}]


def test_a_published_machine_without_a_harness_counts_it_as_zero():
    published = {"machines": {"master": {"claude": 2}}, "silent": [], "measured_at": NOW}
    data = viewed(Store({}), *ports_for([ROOT], {}, published=published))
    assert data["agents"] == {"total": 2, "source": "herdr", "reporting": 1, "of": 1, "claude": 2, "codex": 0}
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": 2}]


def test_a_reading_where_no_machine_answers_falls_back_to_hive_counts():
    published = {"machines": {}, "silent": ["master"], "measured_at": NOW}
    readings = {"root": {**HOST, "agents": {"claude": 3, "codex": 1}}}
    data = viewed(Store({}), *ports_for([ROOT], readings, published=published))
    assert data["agents"] == {"total": 4, "source": "hives", "reporting": 1, "of": 1, "claude": 3, "codex": 1}
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": 4}]


def test_a_silent_master_entry_keeps_the_swarm_master_name():
    published = {"machines": {"hive-1": {"claude": 1}}, "silent": ["master"], "measured_at": NOW}
    data = viewed(Store({}), *ports_for([ROOT], {}, published=published))
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": None}, {"name": "hive-1", "agents": 1}]


def test_the_bridge_is_read_from_each_live_interface_hive_in_the_store_redis_at_the_request_time():
    store, reads = Store({}), []
    second = {**ROOT, "id": "desk", "name": "desk"}
    viewed(store, *ports_for([POD_HIVE, second, ROOT, {**ROOT, "id": "gone", "live": False}], {}, reads=reads))
    assert reads == [(store.redis, "desk", NOW), (store.redis, "root", NOW)]


def test_a_reading_on_a_headless_hive_is_not_the_bridge():
    published = {"machines": {"master": {"claude": 2}}, "silent": [], "measured_at": NOW}
    data = viewed(Store({}), *ports_for([{**POD_HIVE, "id": "root"}], {}, published=published))
    assert data["agents"]["source"] == "hives"


def test_route_reads_the_view_at_the_current_millisecond(monkeypatch):
    seen = []
    monkeypatch.setattr(monitor.time, "time", lambda: 1234.5)
    monkeypatch.setattr(monitor, "view", lambda store, now_ms: seen.append((store, now_ms)) or {"agents": 1})
    reply = monitor.route(SimpleNamespace(swarm=SimpleNamespace(store=lambda: "store")))
    assert seen == [("store", 1_234_500)]
    assert reply == {"data": {"agents": 1}, "revision": monitor.resources.revision({"agents": 1})}


def test_a_swarm_store_error_answers_503_storage_error():
    services = SimpleNamespace(swarm=SimpleNamespace(store=mock.Mock(side_effect=monitor.SwarmError("no store"))))
    with pytest.raises(APIError) as caught:
        monitor.route(services)
    assert (caught.value.status, caught.value.code, str(caught.value)) == (503, "storage_error", "no store")


def test_two_live_interface_hives_merge_into_one_swarm_master_row():
    stale = {"id": "old-id", "name": "swarm-master", "ui": "yes", "live": True}
    readings = {
        "root": {**HOST, "agents": {"claude": 6, "codex": 0}},
        "old-id": {**HOST, "agents": {"claude": 0, "codex": 0}},
    }
    data = viewed(Store({}), *ports_for([ROOT, stale], readings))
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": 6}]
    assert data["hives"]["live"] == 1
    assert data["agents"] == {"total": 6, "source": "hives", "reporting": 1, "of": 1, "claude": 6, "codex": 0}


def test_a_herdr_label_matching_a_live_hive_shows_its_current_name_not_its_raw_id():
    worker = {"id": "06db8fa06259f56a", "name": "worker-one", "ui": "no", "live": True}
    published = {
        "machines": {"master": {"claude": 6}, "06db8fa06259f56a": {"claude": 7}},
        "silent": [],
        "measured_at": NOW,
    }
    data = viewed(Store({}), *ports_for([ROOT, worker], {}, published=published))
    assert data["hives"]["rows"] == [
        {"name": "swarm-master", "agents": 6},
        {"name": "worker-one", "agents": 7},
    ]


def test_a_herdr_label_matching_a_hive_no_longer_live_is_not_listed():
    dead = {"id": "dead-id", "name": "dead-hive", "ui": "no", "live": False}
    published = {
        "machines": {"master": {"claude": 6}, "dead-id": {"claude": 9}},
        "silent": [],
        "measured_at": NOW,
    }
    data = viewed(Store({}), *ports_for([ROOT, dead], {}, published=published))
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": 6}]
    assert data["agents"] == {"total": 6, "source": "herdr", "reporting": 1, "of": 1, "claude": 6, "codex": 0}


@pytest.mark.feature("monitor-every-interface-hive")
def test_a_herdr_name_of_a_hive_no_longer_live_is_skipped_and_the_next_machine_still_counts() -> None:
    dead = {"id": "dead-id", "name": "dead-hive", "ui": "no", "live": False}
    published = {
        "machines": {"dead-hive": {"claude": 9}, "spare": {"claude": 2}, "master": {"claude": 6}},
        "silent": [],
        "measured_at": NOW,
    }
    data = viewed(Store({}), *ports_for([ROOT, dead], {}, published=published))
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": 6}, {"name": "spare", "agents": 2}]
    assert data["agents"] == {"total": 8, "source": "herdr", "reporting": 2, "of": 2, "claude": 8, "codex": 0}


DESK = {"id": "desk", "name": "swarm-master", "ui": "yes", "live": True}
HIVE_003 = {"id": "w3", "name": "swarm-hive-003", "ui": "no", "live": True}
ROOT_BRIDGE = {
    "machines": {"master": {"claude": 3}, "swarm-hive-001": {"claude": 1}, "swarm-hive-003": {"claude": 8, "codex": 1}},
    "silent": [],
    "measured_at": NOW,
}


@pytest.mark.feature("monitor-every-interface-hive")
@pytest.mark.parametrize("order", ["desk first", "root first"])
def test_every_live_interface_hives_herdr_reading_is_counted_so_every_live_hive_shows(order: str) -> None:
    interface = [DESK, {**ROOT, "name": "swarm-master"}]
    hives = [*(interface if order == "desk first" else interface[::-1]), POD_HIVE, HIVE_003]
    bridges = {"desk": {"machines": {"master": {"claude": 5}}, "silent": [], "measured_at": NOW}, "root": ROOT_BRIDGE}
    data = viewed(Store({}), *ports_for(hives, {}, bridges=bridges))
    assert data["agents"] == {"total": 18, "source": "herdr", "reporting": 3, "of": 3, "claude": 17, "codex": 1}
    assert data["hives"]["rows"] == [
        {"name": "swarm-master", "agents": 8},
        {"name": "swarm-hive-001", "agents": 1},
        {"name": "swarm-hive-003", "agents": 9},
    ]


@pytest.mark.feature("monitor-every-interface-hive")
@pytest.mark.parametrize("order", ["desk first", "root first"])
def test_a_hive_two_readings_count_is_counted_once_from_its_own_reading(order: str) -> None:
    root_bridge = {**ROOT_BRIDGE, "machines": {**ROOT_BRIDGE["machines"], "desk": {"claude": 2}}}
    bridges = {"desk": {"machines": {"master": {"claude": 5}}, "silent": [], "measured_at": NOW}, "root": root_bridge}
    interface = [{**DESK, "name": "desk"}, ROOT]
    hives = interface if order == "desk first" else interface[::-1]
    data = viewed(Store({}), *ports_for(hives, {}, bridges=bridges))
    assert sorted(data["hives"]["rows"], key=lambda row: row["name"]) == [
        {"name": "swarm-hive-001", "agents": 1},
        {"name": "swarm-hive-003", "agents": 9},
        {"name": "swarm-master", "agents": 8},
    ]
    assert data["agents"]["total"] == 18


@pytest.mark.feature("monitor-every-interface-hive")
@pytest.mark.parametrize("order", ["desk first", "root first"])
def test_a_hive_silent_on_its_own_reading_keeps_the_count_another_reading_gives(order: str) -> None:
    root_bridge = {**ROOT_BRIDGE, "machines": {"master": {"claude": 3}, "desk": {"claude": 2}}}
    bridges = {"desk": {"machines": {}, "silent": ["master"], "measured_at": NOW}, "root": root_bridge}
    interface = [{**DESK, "name": "desk"}, ROOT]
    hives = interface if order == "desk first" else interface[::-1]
    data = viewed(Store({}), *ports_for(hives, {}, bridges=bridges))
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": 5}]
    assert data["agents"] == {"total": 5, "source": "herdr", "reporting": 1, "of": 1, "claude": 5, "codex": 0}
