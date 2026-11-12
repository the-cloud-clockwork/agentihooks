from types import SimpleNamespace
from typing import Any
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
ROOT = {"id": "root", "name": "swarm-master", "ui": "yes", "live": True}
POD_HIVE = {"id": "pod", "name": "swarm-hive-001", "ui": "no", "live": True}
AUTOMATION = {
    "id": "068ce20686954481",
    "name": "swarm-master",
    "ui": "yes",
    "live": True,
    "members": {"automation-vm": {"heartbeat_at": NOW, "draining": "no"}},
}


class Redis:
    pass


class Store:
    def __init__(self, swarms: dict[str, str], agents: dict[str, list[SimpleNamespace]] | None = None) -> None:
        self.swarms = swarms
        self.redis = Redis()
        self.registered = agents or {}

    def slugs(self) -> list[str]:
        return list(self.swarms)

    def config(self, slug: str) -> SimpleNamespace:
        return SimpleNamespace(state=self.swarms[slug])

    def agents(self, slug: str) -> list[SimpleNamespace]:
        return self.registered.get(slug, [])


def seat(name: str, harness: str = "claude", hive: str = "", state: str = "working", host: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        harness=harness,
        hive=hive,
        state=state,
        runtime_backend="local",
        runtime_target={"host_id": host} if host else {},
    )


def ports_for(
    hives: list[dict[str, Any]], readings: dict[str, dict], here: str = "root", published: dict | None = None
) -> tuple:
    registry = SimpleNamespace(
        hives=lambda redis: hives if isinstance(redis, Redis) else [],
        live=lambda hive, now: hive["live"] and now == NOW,
    )
    headroom = SimpleNamespace(
        read=lambda redis, hive, now: readings.get(hive) if isinstance(redis, Redis) and now == NOW else None
    )
    herdr_stats = SimpleNamespace(read=lambda redis, hive, now: published, MASTER="master")
    reports = SimpleNamespace(host=lambda agent: agent.runtime_target.get("host_id", ""))
    swarm = SimpleNamespace(commands=SimpleNamespace(hive_id=lambda: here))
    return (
        mock.patch.object(
            monitor.ports,
            "hive",
            return_value=SimpleNamespace(
                registry=registry, headroom=headroom, herdr_stats=herdr_stats, reports=reports
            ),
        ),
        mock.patch.object(monitor.ports, "swarm", return_value=swarm),
    )


def viewed(store: Store, *patches: Any) -> dict:
    with patches[0], patches[1]:
        return monitor.view(store, NOW)


def test_a_bare_host_counts_its_load_and_free_memory() -> None:
    assert monitor.capacity(HOST) == (6.0, 16, 16000, 36000)


def test_a_pod_is_held_to_its_own_cpu_and_memory_limits() -> None:
    assert monitor.capacity(POD) == (1.5, 4.0, 8000, 12000)


def test_load_over_the_core_count_reads_as_every_core_busy() -> None:
    assert monitor.capacity({**HOST, "load5": 40.0})[0] == 16


def test_a_pod_without_a_load_reading_keeps_the_node_load_under_its_core_limit() -> None:
    assert monitor.capacity({**HOST, "load5": 2.0, "pod_cpus": 4.0})[:2] == (2.0, 4.0)


def test_a_pod_memory_limit_without_its_available_figure_keeps_the_node_memory() -> None:
    assert monitor.capacity({**HOST, "pod_total_mb": 1000}) == (6.0, 16, 16000, 36000)


def test_more_memory_available_than_the_total_reads_as_none_in_use() -> None:
    assert monitor.capacity({**HOST, "available_mb": 40000})[2] == 0


def test_the_monitor_route_answers_get_with_the_services_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm_ledger.api import routes

    services = object()
    monkeypatch.setattr(monitor, "route", lambda given: {"data": given})
    handler = SimpleNamespace(command="GET", path="/api/v1/monitor")
    assert routes.global_resource(handler, services, ["monitor"]) == {"data": services}


def test_no_reading_gives_zero_cpu_and_memory() -> None:
    data = viewed(Store({}), *ports_for([], {}))
    assert (data["cpu"], data["memory"]) == ({"used": 0, "cores": 0}, {"used_mb": 0, "total_mb": 0})


def test_cpu_figures_are_rounded_to_one_decimal() -> None:
    reading = {**POD, "pod_cpus": 4.25, "pod_load": 1.234}
    data = viewed(Store({}), *ports_for([POD_HIVE], {"pod": reading}))
    assert data["cpu"] == {"used": 1.2, "cores": 4.2}


def test_cpu_memory_and_running_swarms_come_from_live_hives_and_the_store() -> None:
    store = Store({"a": "running", "b": "stopped", "c": "running"})
    hives = [POD_HIVE, ROOT, {"id": "gone", "name": "gone", "live": False}]
    readings = {"root": HOST, "pod": POD, "gone": HOST}
    data = viewed(store, *ports_for(hives, readings))
    assert data["swarms"] == {"running": 2}
    assert data["hives"]["measured"] == 2
    assert data["cpu"] == {"used": 7.5, "cores": 20.0}
    assert data["memory"] == {"used_mb": 24000, "total_mb": 48000}


def test_an_unreachable_store_answers_503() -> None:
    from redis import RedisError

    services = SimpleNamespace(swarm=SimpleNamespace(store=mock.Mock(side_effect=RedisError("down"))))
    with pytest.raises(APIError) as caught:
        monitor.route(services)
    assert (caught.value.status, caught.value.code, str(caught.value)) == (
        503,
        "storage_error",
        "The swarm store is unreachable",
    )


def test_route_reads_the_view_at_the_current_millisecond(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(monitor.time, "time", lambda: 1234.5)
    monkeypatch.setattr(monitor, "view", lambda store, now_ms: seen.append((store, now_ms)) or {"agents": 1})
    reply = monitor.route(SimpleNamespace(swarm=SimpleNamespace(store=lambda: "store")))
    assert seen == [("store", 1_234_500)]
    assert reply == {"data": {"agents": 1}, "revision": monitor.resources.revision({"agents": 1})}


def test_a_swarm_store_error_answers_503_storage_error() -> None:
    services = SimpleNamespace(swarm=SimpleNamespace(store=mock.Mock(side_effect=monitor.SwarmError("no store"))))
    with pytest.raises(APIError) as caught:
        monitor.route(services)
    assert (caught.value.status, caught.value.code, str(caught.value)) == (503, "storage_error", "no store")


@pytest.mark.feature("monitor-counts-registered-seats")
def test_every_row_counts_registered_seats_masters_included_never_an_unregistered_pane() -> None:
    published = {"machines": {"master": {"claude": 4, "codex": 1}}, "silent": [], "measured_at": NOW}
    readings = {"root": {**HOST, "agents": {"claude": 5}}, AUTOMATION["id"]: {**HOST, "agents": {"claude": 9}}}
    registered = {
        "one": [
            seat("master@one", hive="root"),
            seat("engineer@1", hive="root"),
            seat("ci@1", "codex", "root"),
            seat("engineer@2", hive=AUTOMATION["id"]),
            seat("engineer@3", hive="root", state="finished"),
        ],
        "two": [seat("master@two"), seat("engineer@4", "codex", "pod")],
    }
    store = Store({"one": "running", "two": "running"}, registered)
    data = viewed(store, *ports_for([ROOT, AUTOMATION, POD_HIVE], readings, published=published))
    assert data["hives"]["rows"] == [
        {"name": "swarm-master", "agents": 4},
        {"name": "automation-vm", "agents": 1},
        {"name": "swarm-hive-001", "agents": 1},
    ]
    assert data["agents"] == {"total": 6, "source": "seats", "reporting": 3, "of": 3, "claude": 4, "codex": 2}


@pytest.mark.feature("monitor-counts-registered-seats")
def test_a_live_hive_with_no_seat_shows_zero_never_a_missing_count() -> None:
    data = viewed(Store({"one": "running"}, {"one": [seat("master@one")]}), *ports_for([ROOT, POD_HIVE], {}))
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": 1}, {"name": "swarm-hive-001", "agents": 0}]
    assert data["hives"]["live"] == 2


@pytest.mark.feature("monitor-counts-registered-seats")
def test_a_seat_on_a_hive_no_longer_live_keeps_its_registered_name_and_an_unknown_hive_its_id() -> None:
    gone = {"id": "gone-id", "name": "old-hive", "ui": "no", "live": False}
    registered = {"one": [seat("engineer@1", hive="gone-id"), seat("engineer@2", hive="stray")]}
    data = viewed(Store({"one": "running"}, registered), *ports_for([ROOT, gone], {}))
    assert data["hives"]["rows"] == [
        {"name": "swarm-master", "agents": 0},
        {"name": "old-hive", "agents": 1},
        {"name": "stray", "agents": 1},
    ]
    assert data["agents"]["total"] == 2


@pytest.mark.feature("monitor-counts-registered-seats")
def test_a_seat_without_a_hive_is_placed_by_its_runtime_target_then_on_this_hive() -> None:
    registered = {"one": [seat("engineer@1", host="pod"), seat("engineer@2")]}
    data = viewed(Store({"one": "running"}, registered), *ports_for([ROOT, POD_HIVE], {}))
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": 1}, {"name": "swarm-hive-001", "agents": 1}]


@pytest.mark.feature("monitor-counts-registered-seats")
def test_a_seat_that_records_no_harness_counts_as_claude() -> None:
    registered = {"one": [seat("engineer@1", harness=""), seat("engineer@2", harness="codex")]}
    data = viewed(Store({"one": "running"}, registered), *ports_for([ROOT], {}))
    assert data["agents"] == {"total": 2, "source": "seats", "reporting": 1, "of": 1, "claude": 1, "codex": 1}


@pytest.mark.feature("monitor-row-per-live-hive")
def test_two_live_interface_hives_without_members_keep_a_row_each() -> None:
    second = {"id": "old-id", "name": "swarm-master", "ui": "yes", "live": True}
    registered = {"one": [seat(f"engineer@{n}", hive="root") for n in range(6)]}
    data = viewed(Store({"one": "running"}, registered), *ports_for([ROOT, second], {}))
    assert data["hives"]["rows"] == [{"name": "swarm-master", "agents": 0}, {"name": "swarm-master", "agents": 6}]
    assert data["hives"]["live"] == 2
    assert data["agents"] == {"total": 6, "source": "seats", "reporting": 2, "of": 2, "claude": 6, "codex": 0}


@pytest.mark.feature("monitor-row-per-live-hive")
@pytest.mark.parametrize("order", ["workstation first", "automation first"])
def test_two_interface_hives_sharing_a_name_show_a_row_each_from_their_seats(order: str) -> None:
    interface = [ROOT, AUTOMATION]
    hives = [*(interface if order == "workstation first" else interface[::-1]), POD_HIVE]
    registered = {
        "one": [
            *(seat(f"engineer@w{n}", hive="root") for n in range(2)),
            *(seat(f"engineer@a{n}", hive=AUTOMATION["id"]) for n in range(9)),
            seat("ci@1", "codex", "pod"),
        ]
    }
    data = viewed(Store({"one": "running"}, registered), *ports_for(hives, {}))
    assert data["hives"]["rows"] == [
        {"name": "swarm-master", "agents": 2},
        {"name": "automation-vm", "agents": 9},
        {"name": "swarm-hive-001", "agents": 1},
    ]
    assert data["agents"] == {"total": 12, "source": "seats", "reporting": 3, "of": 3, "claude": 11, "codex": 1}


@pytest.mark.feature("monitor-row-per-live-hive")
def test_a_hive_presence_reports_twice_is_one_row_and_two_hives_sharing_a_name_are_two() -> None:
    present = [
        {"id": "a", "name": "twin", "agents": {"claude": 1}},
        {"id": "b", "name": "twin", "agents": {"claude": 2}},
        {"id": "a", "name": "twin", "agents": {"codex": 3}},
    ]
    agents, rows = monitor.tally("otel", [(hive["id"], hive["name"], hive["agents"]) for hive in present])
    assert rows == [{"name": "twin", "agents": 4}, {"name": "twin", "agents": 2}]
    assert agents == {"total": 6, "source": "otel", "reporting": 2, "of": 2, "claude": 3, "codex": 3}


@pytest.mark.feature("monitor-row-per-live-hive")
def test_only_a_hive_whose_name_another_shares_is_shown_by_its_sorted_member_names() -> None:
    beat = {"heartbeat_at": NOW, "draining": "no"}
    live = [
        {"id": "a", "name": "twin", "ui": "no", "members": {"vm-b": beat, "vm-a": beat}},
        {"id": "b", "name": "twin", "ui": "no"},
        {"id": "c", "name": "solo", "ui": "no", "members": {"pod-c": beat}},
        {"id": "d", "name": "desk", "ui": "yes"},
    ]
    assert monitor.labels(live) == {"a": "vm-a, vm-b", "b": "twin", "c": "solo", "d": "swarm-master"}
