import threading

import pytest

from scripts.hive import admission
from tests.swarm.test_cli import env as env

pytestmark = pytest.mark.unit

GB = 1024


def reading(cpus=16, load5=0.0, available_mb=60 * GB, total_mb=64 * GB, **fields):
    return {"cpus": cpus, "load5": load5, "available_mb": available_mb, "total_mb": total_mb, **fields}


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


@pytest.fixture
def redis(server):
    import fakeredis

    return fakeredis.FakeRedis(server=server, decode_responses=True)


def test_an_idle_hive_has_room_until_projected_cpu_passes_eighty_percent():
    found = admission.room(reading(cpus=16, load5=0.0))
    assert found == admission.Room(
        25,
        admission.CPU,
        "five minute load 0.00 on 16 CPUs, 0.5 CPU per agent, fits 25 more under the 80 percent line",
    )


def test_a_loaded_hive_refuses_on_cpu():
    found = admission.room(reading(cpus=8, load5=6.2))
    assert (found.room, found.reason) == (0, admission.CPU)
    assert admission.refusal(found, 0) == (
        "cpu: five minute load 6.20 on 8 CPUs, 0.5 CPU per agent, fits 0 more under the 80 percent line"
    )


def test_a_hive_short_of_memory_refuses_on_ram():
    found = admission.room(reading(cpus=64, available_mb=13 * GB, total_mb=64 * GB))
    assert found == admission.Room(
        0,
        admission.RAM,
        "13312 of 65536 MB available, 700 MB per agent, fits 0 more under the 80 percent line",
    )
    assert admission.refusal(found, 0).startswith("ram: 13312 of 65536 MB available")


def test_ram_room_counts_whole_agents_above_the_twenty_percent_floor():
    found = admission.room(reading(cpus=64, available_mb=13108 + 2100, total_mb=65536))
    assert (found.room, found.reason) == (3, admission.RAM)


def test_cpu_room_rounds_down_to_whole_agents():
    found = admission.room(reading(cpus=4, load5=2.1, total_mb=10**6, available_mb=10**6))
    assert (found.room, found.reason) == (2, admission.CPU)


@pytest.mark.parametrize("measured", [None, {"cpus": 4, "load5": 0.0, "available_mb": 1000}])
def test_a_machine_without_a_full_reading_refuses_on_missing_telemetry(measured):
    found = admission.room(measured)
    assert found == admission.Room(0, admission.MISSING, "no fresh headroom reading")
    assert admission.refusal(found, 0) == "missing telemetry: no fresh headroom reading"


def test_the_workstation_keeps_swarm_agents_to_forty_percent_of_its_cpu():
    found = admission.room(reading(cpus=16, load5=0.0), swarm_agents=10)
    assert found == admission.Room(
        2,
        admission.CPU,
        "10 swarm agents at 0.5 CPU each on 16 CPUs, fits 2 more under the 40 percent swarm share",
    )


def test_the_workstation_keeps_swarm_agents_to_forty_percent_of_its_ram():
    found = admission.room(reading(cpus=256, available_mb=30 * GB, total_mb=32 * GB), swarm_agents=16)
    assert found == admission.Room(
        2,
        admission.RAM,
        "16 swarm agents at 700 MB each in 32768 MB, fits 2 more under the 40 percent swarm share",
    )


def test_the_workstation_host_line_wins_when_it_is_tighter_than_the_swarm_share():
    found = admission.room(reading(cpus=16, load5=12.0), swarm_agents=0)
    assert (found.room, found.reason) == (1, admission.CPU)
    assert "80 percent line" in found.detail


def test_the_workstation_swarm_share_never_goes_negative():
    assert admission.room(reading(cpus=16), swarm_agents=40).room == 0


def test_pending_reservations_use_up_the_room():
    found = admission.room(reading(cpus=8, load5=5.6))
    assert found.room == 1
    assert admission.refusal(found, 0) == ""
    assert admission.refusal(found, 1).startswith("cpu: ")


def test_a_reservation_holds_until_the_room_is_spent(redis):
    assert admission.reserve(redis, "anton", "s/one", 2) == (True, 0)
    assert admission.reserve(redis, "anton", "s/two", 2) == (True, 1)
    assert admission.reserve(redis, "anton", "s/three", 2) == (False, 2)
    assert admission.reserve(redis, "anton", "s/one", 2) == (True, 2)
    assert admission.reserve(redis, "vm", "s/three", 2) == (True, 0)
    assert admission.pending(redis, "anton") == 2


def test_a_released_reservation_frees_its_room(redis):
    admission.reserve(redis, "anton", "s/one", 1)
    admission.release(redis, "anton", "s/one")
    assert admission.pending(redis, "anton") == 0
    assert admission.reserve(redis, "anton", "s/two", 1) == (True, 0)


def test_a_reservation_expires_after_its_lifetime(redis):
    assert admission.reserve(redis, "anton", "s/one", 1, ttl_ms=1) == (True, 0)
    import time

    time.sleep(0.01)
    assert admission.pending(redis, "anton") == 0
    assert admission.reserve(redis, "anton", "s/two", 1) == (True, 0)


def test_a_reservation_key_lives_in_the_swarm_keyspace():
    from scripts.swarm.keyspace import ROOT

    assert admission.key("anton") == f"{ROOT}:admission:anton"


def test_a_reservation_rejects_a_bad_lifetime(redis):
    with pytest.raises(ValueError, match="^invalid admission lifetime$"):
        admission.reserve(redis, "anton", "s/one", 1, ttl_ms=0)


def test_concurrent_reservations_never_pass_the_room(server):
    import fakeredis

    room, callers = 3, 12
    barrier = threading.Barrier(callers)
    results = []

    def launch(n):
        client = fakeredis.FakeRedis(server=server, decode_responses=True)
        barrier.wait()
        results.append(admission.reserve(client, "anton", f"s/agent-{n}", room)[0])

    threads = [threading.Thread(target=launch, args=(n,)) for n in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * (callers - room) + [True] * room
    assert admission.pending(fakeredis.FakeRedis(server=server, decode_responses=True), "anton") == room


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        ("no hive for eng has room: anton cpu: five minute load", admission.CPU),
        ("no hive for eng has room: anton ram: 10 of 64 MB", admission.RAM),
        ("no hive for eng has room: anton missing telemetry: no fresh headroom reading", admission.MISSING),
        ("no advertised Claude account has room", admission.QUOTA),
        ("account reservation is full", admission.QUOTA),
        ("worker pool node cap 40 reached", admission.NODE_CAP),
        ("no live hive takes qa", admission.PLACEMENT),
        ("no hive for eng lists the repo agentihooks", admission.PLACEMENT),
    ],
)
def test_every_refusal_falls_in_one_reason(reason, category):
    assert admission.category(reason) == category


def test_status_counts_refusals_by_reason_in_a_fixed_order():
    waits = {
        "a": {"lane": "eng", "reason": "no hive for eng has room: anton ram: low"},
        "b": {"lane": "eng", "reason": "no hive for eng has room: anton cpu: busy"},
        "c": {"lane": "ci", "reason": "no advertised Claude account has room"},
        "d": {"lane": "eng", "reason": "no hive for eng has room: vm cpu: busy"},
    }
    assert admission.status_line(waits) == "admission refusals  cpu 2  ram 1  quota 1"
    assert admission.status_line({}) == ""
    assert admission.REASONS == ("cpu", "ram", "quota", "placement", "node cap", "missing telemetry")


def test_status_report_carries_refusal_counts_for_the_ledger_page(env):
    from scripts.hive import placement_waits
    from scripts.swarm import cli, status

    store, _, _ = env
    cli.main(["sw", "create", "--repo", "/repo"])
    assert status.status_report(store, "sw", {"tasks": []})["admission"] == {}
    placement_waits.observe(store, "sw", "eng", "t1", "no hive for eng has room: anton cpu: busy", 1)
    placement_waits.observe(store, "sw", "ci", "t2", "no hive for ci has room: vm missing telemetry: none", 1)
    assert status.status_report(store, "sw", {"tasks": [{"id": "t1"}, {"id": "t2"}]})["admission"] == {
        "cpu": 1,
        "missing telemetry": 1,
    }
