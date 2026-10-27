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
    assert admission.refusal(found) == (
        "cpu: five minute load 6.20 on 8 CPUs, 0.5 CPU per agent, fits 0 more under the 80 percent line"
    )


def test_a_hive_short_of_memory_refuses_on_ram():
    found = admission.room(reading(cpus=64, available_mb=13 * GB, total_mb=64 * GB))
    assert found == admission.Room(
        0,
        admission.RAM,
        "13312 of 65536 MB available, 700 MB per agent, fits 0 more under the 80 percent line",
    )
    assert admission.refusal(found).startswith("ram: 13312 of 65536 MB available")


def test_ram_room_counts_whole_agents_above_the_twenty_percent_floor():
    found = admission.room(reading(cpus=64, available_mb=13108 + 2100, total_mb=65536))
    assert (found.room, found.reason) == (3, admission.RAM)


def test_cpu_room_rounds_down_to_whole_agents():
    found = admission.room(reading(cpus=4, load5=2.1, total_mb=10**6, available_mb=10**6))
    assert (found.room, found.reason) == (2, admission.CPU)


@pytest.mark.parametrize("measured", [None, {"available_mb": 1000, "total_mb": 2000}, {"cpus": 4}, {"load5": 0.0}])
def test_a_machine_without_cpu_or_load_refuses_on_missing_telemetry(measured):
    found = admission.room(measured)
    assert found == admission.Room(0, admission.MISSING, "no fresh headroom reading")
    assert admission.refusal(found) == "missing telemetry: no fresh headroom reading"


def test_a_reading_from_a_hive_not_yet_republishing_total_mb_is_read_on_cpu_alone():
    found = admission.room({"cpus": 4, "load5": 0.0, "available_mb": 1000})
    assert (found.room, found.reason) == (6, admission.CPU)
    assert admission.refusal(found) == ""


def test_swarm_share_is_skipped_on_a_reading_without_total_mb():
    found = admission.room({"cpus": 16, "load5": 0.0, "available_mb": 1000}, swarm_agents=10)
    assert (found.room, found.reason) == (25, admission.CPU)


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


def test_the_swarm_share_never_binds_below_the_hive_max_agents_on_a_quiet_host():
    found = admission.room(reading(cpus=16, load5=4.32), swarm_agents=12, max_agents=20)
    assert found == admission.Room(
        16,
        admission.CPU,
        "five minute load 4.32 on 16 CPUs, 0.5 CPU per agent, fits 16 more under the 80 percent line",
    )


def test_the_swarm_share_still_binds_when_it_equals_the_hive_slots_left():
    found = admission.room(reading(cpus=16), swarm_agents=10, max_agents=12)
    assert found == admission.Room(
        2,
        admission.CPU,
        "10 swarm agents at 0.5 CPU each on 16 CPUs, fits 2 more under the 40 percent swarm share",
    )


def test_the_host_cpu_line_still_binds_under_the_hive_max_agents():
    found = admission.room(reading(cpus=16, load5=12.0), swarm_agents=12, max_agents=20)
    assert (found.room, found.reason) == (1, admission.CPU)
    assert found.detail == "five minute load 12.00 on 16 CPUs, 0.5 CPU per agent, fits 1 more under the 80 percent line"


def test_the_host_ram_line_still_binds_under_the_hive_max_agents():
    found = admission.room(reading(cpus=64, available_mb=13 * GB, total_mb=64 * GB), swarm_agents=12, max_agents=20)
    assert found == admission.Room(
        0,
        admission.RAM,
        "13312 of 65536 MB available, 700 MB per agent, fits 0 more under the 80 percent line",
    )


def test_pending_reservations_use_up_the_room():
    found = admission.room(reading(cpus=8, load5=5.6))
    assert found.room == 1
    assert admission.refusal(found) == ""
    held = admission.room(reading(cpus=8, load5=5.6), pending=1)
    assert admission.refusal(held) == (
        "cpu: five minute load 5.60 on 8 CPUs, 0.5 CPU per agent with 1 pending launches, "
        "fits 0 more under the 80 percent line"
    )


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


def test_settling_a_reservation_only_shortens_one_that_is_held(redis):
    import time

    admission.reserve(redis, "anton", "s/one", 2)
    admission.reserve(redis, "anton", "s/two", 2, ttl_ms=1000)
    admission.settle(redis, "anton", "s/one")
    admission.settle(redis, "anton", "s/two")
    admission.settle(redis, "anton", "s/absent")
    now = time.time() * 1000
    assert now < redis.zscore(admission.key("anton"), "s/one") <= now + admission.SETTLE_MS
    assert redis.zscore(admission.key("anton"), "s/one").is_integer()
    assert redis.zscore(admission.key("anton"), "s/two") <= now + 1000
    assert redis.zscore(admission.key("anton"), "s/absent") is None


def test_a_reservation_key_lives_in_the_swarm_keyspace():
    from scripts.swarm.keyspace import ROOT

    assert admission.key("anton") == f"{ROOT}:admission:anton"


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
        ("no hive for eng lists the repo quota-tools", admission.PLACEMENT),
        ("quota: every account is past its handoff line", admission.QUOTA),
        ("no hive for eng advertises the claude slot account-two", admission.PLACEMENT),
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
    assert admission.status_lines(waits) == ["admission refusals  cpu 2  ram 1  quota 1"]
    assert admission.status_lines({}) == []
    assert admission.REASONS == ("cpu", "ram", "quota", "placement", "node cap", "max agents", "missing telemetry")


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


def test_pending_launches_lower_only_the_host_line_the_load_has_not_shown_yet():
    measured = reading(cpus=10, load5=2.0)
    assert admission.room(measured, 0) == admission.Room(
        8, admission.CPU, "0 swarm agents at 0.5 CPU each on 10 CPUs, fits 8 more under the 40 percent swarm share"
    )
    assert admission.room(measured, 0, pending=3).room == 8
    assert admission.room(measured, 0, pending=5) == admission.Room(
        7,
        admission.CPU,
        "five minute load 2.00 on 10 CPUs, 0.5 CPU per agent with 5 pending launches, fits 7 more under the 80 percent line",
    )
    assert admission.room(measured, 6, pending=1).room == 2


def test_a_reservation_holds_for_three_five_minute_load_windows(redis):
    assert admission.PENDING_MS == 900_000
    seconds, micros = redis.time()
    admission.reserve(redis, "anton", "s/one", 1)
    expiry = redis.zscore(admission.key("anton"), "s/one") - (int(seconds) * 1000 + int(micros) // 1000)
    assert 899_000 <= expiry <= 901_000
    assert 899_000 <= redis.pttl(admission.key("anton")) <= 900_000


def test_a_recorded_launch_counts_even_past_the_room(redis):
    assert admission.reserve(redis, "box", "s/one", 1) == (True, 0)
    seconds, micros = redis.time()
    admission.record(redis, "box", "s/two")
    assert admission.pending(redis, "box") == 2
    expiry = redis.zscore(admission.key("box"), "s/two") - (int(seconds) * 1000 + int(micros) // 1000)
    assert 899_000 <= expiry <= 901_000
    assert 899_000 <= redis.pttl(admission.key("box")) <= 900_000


def test_the_ram_line_counts_whole_agents_under_eighty_percent_less_pending_launches():
    measured = reading(cpus=64, available_mb=24_000, total_mb=100_000)
    line = "700 MB per agent{}, fits {} more under the 80 percent line"
    assert admission.room(measured) == admission.Room(
        5, admission.RAM, "24000 of 100000 MB available, " + line.format("", 5)
    )
    assert admission.room(measured, pending=2) == admission.Room(
        3, admission.RAM, "24000 of 100000 MB available, " + line.format(" with 2 pending launches", 3)
    )


def test_a_tie_names_the_host_line_first():
    assert admission.room(reading(cpus=10, load5=4.0), 0) == admission.Room(
        8,
        admission.CPU,
        "five minute load 4.00 on 10 CPUs, 0.5 CPU per agent, fits 8 more under the 80 percent line",
    )


def test_an_overloaded_host_floors_its_cpu_line_before_comparing_with_ram():
    measured = reading(cpus=1, load5=1.55, available_mb=1000, total_mb=10_000)
    assert admission.room(measured) == admission.Room(
        0, admission.CPU, "five minute load 1.55 on 1 CPUs, 0.5 CPU per agent, fits 0 more under the 80 percent line"
    )


def test_the_swarm_ram_share_counts_whole_agents_under_forty_percent():
    measured = reading(cpus=64, available_mb=87_500, total_mb=87_500)
    assert admission.room(measured, 0) == admission.Room(
        50, admission.RAM, "0 swarm agents at 700 MB each in 87500 MB, fits 50 more under the 40 percent swarm share"
    )


def pod(**fields):
    return reading(cpus=16, available_mb=28 * GB, total_mb=30 * GB, **fields)


def test_a_pod_memory_limit_binds_below_the_node_memory():
    measured = pod(pod_total_mb=6300, pod_available_mb=5300, pod_cpus=4.0, pod_load=0.0)
    assert admission.room(measured) == admission.Room(
        5, admission.RAM, "pod 5300 of 6300 MB available, 700 MB per agent, fits 5 more under the 80 percent line"
    )
    assert admission.room(measured, pending=2) == admission.Room(
        3,
        admission.RAM,
        "pod 5300 of 6300 MB available, 700 MB per agent with 2 pending launches, "
        "fits 3 more under the 80 percent line",
    )


def test_a_pod_cpu_limit_counts_the_pod_load_below_the_node_cpus():
    measured = pod(pod_total_mb=6300, pod_available_mb=5300, pod_cpus=2.5, pod_load=0.6)
    assert admission.room(measured, pending=1) == admission.Room(
        1,
        admission.CPU,
        "pod load 0.60 on a 2.5 CPU limit, 0.5 CPU per agent with 1 pending launches, "
        "fits 1 more under the 80 percent line",
    )
    assert admission.room({**measured, "pod_load": 0.0}, pending=1).room == 3


def test_a_pod_limit_binds_alone_and_needs_both_of_its_fields():
    assert admission.room(pod(pod_cpus=1.0, pod_load=0.0)).detail == (
        "pod load 0.00 on a 1 CPU limit, 0.5 CPU per agent, fits 1 more under the 80 percent line"
    )
    assert admission.room(pod(pod_total_mb=1400, pod_available_mb=1400)).room == 1
    for partial in ({"pod_cpus": 1.0}, {"pod_load": 0.0}, {"pod_available_mb": 0}, {"pod_total_mb": 0}):
        assert admission.room(pod(**partial)).room == admission.room(pod()).room == 25


def test_the_node_binds_when_it_is_tighter_than_the_pod():
    measured = pod(load5=12.0, pod_total_mb=64 * GB, pod_available_mb=64 * GB, pod_cpus=16.0, pod_load=0.0)
    assert admission.room(measured) == admission.Room(
        1, admission.CPU, "five minute load 12.00 on 16 CPUs, 0.5 CPU per agent, fits 1 more under the 80 percent line"
    )


def test_a_full_pod_refuses_on_its_memory_and_floors_at_zero():
    found = admission.room(pod(pod_total_mb=6300, pod_available_mb=600))
    assert (found.room, found.reason) == (0, admission.RAM)
    assert admission.refusal(found) == (
        "ram: pod 600 of 6300 MB available, 700 MB per agent, fits 0 more under the 80 percent line"
    )


def test_a_hive_at_its_max_agents_refuses_on_the_cap_whatever_its_headroom():
    found = admission.capped(admission.Room(5, admission.RAM, "fits 5 more"), 3, 3)
    assert found == admission.Room(0, admission.MAX_AGENTS, "3 of 3 agents on the hive, fits 0 more")
    assert admission.refusal(found) == "max agents: 3 of 3 agents on the hive, fits 0 more"


def test_a_hive_past_its_max_agents_floors_its_room_at_zero():
    assert admission.capped(admission.Room(5, admission.CPU, "fits 5 more"), 4, 3) == admission.Room(
        0, admission.MAX_AGENTS, "4 of 3 agents on the hive, fits 0 more"
    )


def test_the_max_agents_cap_binds_only_when_tighter_than_the_headroom():
    headroom = admission.Room(2, admission.RAM, "fits 2 more")
    assert admission.capped(headroom, 1, 4) == headroom
    assert admission.capped(headroom, 1, 3) == headroom
    assert admission.capped(headroom, 2, 3) == admission.Room(
        1, admission.MAX_AGENTS, "2 of 3 agents on the hive, fits 1 more"
    )


def test_a_max_agents_refusal_falls_in_its_own_reason():
    assert admission.category("no hive for eng has room: remote max agents: 3 of 3 agents") == admission.MAX_AGENTS
    assert admission.MAX_AGENTS in admission.REASONS
