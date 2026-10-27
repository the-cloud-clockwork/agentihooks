import json
from pathlib import Path

import pytest

from scripts.hive import cli, daemon, headroom, registry
from scripts.hive.orders import Orders

pytestmark = pytest.mark.unit

DEFAULT_CGROUP = headroom.CGROUP

MEMINFO = (
    "MemTotal:       16000000 kB\nMemAvailable:    8192000 kB\nSwapTotal:       2048000 kB\nSwapFree:  1024000 kB\n"
)


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def proc(tmp_path):
    folder = tmp_path / "proc"
    folder.mkdir()
    (folder / "loadavg").write_text("9.10 3.456 1.20 2/300 4000\n")
    (folder / "meminfo").write_text(MEMINFO)
    return folder


@pytest.fixture(autouse=True)
def agents(monkeypatch):
    found = {"claude": {}, "codex": {}}
    monkeypatch.setattr(headroom.account_sessions, "local_processes", lambda proc: found)
    return found


@pytest.fixture(autouse=True)
def cgroup(tmp_path, monkeypatch):
    folder = tmp_path / "cgroup"
    folder.mkdir()
    monkeypatch.setattr(headroom, "CGROUP", folder)
    return folder


def test_measure_reads_the_five_minute_load_and_available_memory(proc, monkeypatch):
    monkeypatch.setattr(headroom.os, "cpu_count", lambda: 8)
    assert headroom.measure(3, 77_000, proc) == {
        "cpus": 8,
        "load5": 3.456,
        "available_mb": 8000,
        "total_mb": 15625,
        "pending": 3,
        "agents": {"claude": 0, "codex": 0},
        "measured_at": 77_000,
    }


def test_measure_counts_the_agent_processes_in_the_process_table_it_reads(proc, agents):
    agents.update({"claude": {11: "a", 12: "b"}, "codex": {13: "c"}})
    assert headroom.measure(0, 1, proc)["agents"] == {"claude": 2, "codex": 1}


def test_measure_rounds_the_memory_total_down_to_whole_megabytes(proc):
    (proc / "meminfo").write_text(MEMINFO.replace("16000000 kB", "16000900 kB"))
    total = headroom.measure(0, 0, proc)["total_mb"]
    assert (type(total), total) == (int, 15625)


def test_measure_counts_one_cpu_when_the_count_is_unknown(proc, monkeypatch):
    monkeypatch.setattr(headroom.os, "cpu_count", lambda: None)
    assert headroom.measure(0, 1, proc)["cpus"] == 1


def test_measure_is_none_without_a_memory_total(proc):
    (proc / "meminfo").write_text(MEMINFO.replace("MemTotal", "MemFree"))
    assert headroom.measure(0, 1, proc) is None


@pytest.mark.parametrize("missing", ["loadavg", "meminfo"])
def test_measure_is_none_when_a_process_file_cannot_be_read(proc, missing):
    (proc / missing).unlink()
    assert headroom.measure(0, 1, proc) is None


@pytest.mark.parametrize(("loadavg", "meminfo"), [("1.0\n", MEMINFO), ("1.0 x 1.0\n", MEMINFO), ("1 2 3\n", "")])
def test_measure_is_none_on_unreadable_numbers(proc, loadavg, meminfo):
    (proc / "loadavg").write_text(loadavg)
    (proc / "meminfo").write_text(meminfo)
    assert headroom.measure(0, 1, proc) is None


def test_publish_counts_waiting_launch_orders_and_expires_with_the_heartbeat(redis, proc, monkeypatch):
    monkeypatch.setattr(headroom.os, "cpu_count", lambda: 4)
    stream = Orders(redis).stream("box")
    redis.xadd(stream, {"order": "{}"})
    redis.xadd(stream, {"order": "{}"})
    redis.xadd(Orders(redis).stream("other"), {"order": "{}"})
    headroom.publish(redis, "box", 5_000, proc)
    assert json.loads(redis.get(f"{registry.key('box')}:headroom")) == {
        "cpus": 4,
        "load5": 3.456,
        "available_mb": 8000,
        "total_mb": 15625,
        "pending": 2,
        "agents": {"claude": 0, "codex": 0},
        "measured_at": 5_000,
    }
    assert 29_000 < redis.pttl(headroom.key("box")) <= 30_000


def test_measure_and_publish_read_the_host_process_files_by_default(redis):
    assert headroom.measure(0, 1)["measured_at"] == 1
    headroom.publish(redis, "box", 2)
    assert json.loads(redis.get(headroom.key("box")))["measured_at"] == 2


def test_publish_writes_nothing_without_a_measurement(redis, proc):
    (proc / "loadavg").unlink()
    headroom.publish(redis, "box", 5_000, proc)
    assert redis.get(headroom.key("box")) is None


def test_read_takes_numbers_up_to_two_heartbeats_old(redis):
    assert headroom.STALE_MS == 2 * daemon.BEAT_SECONDS * 1000 == 30_000
    record = {"cpus": 2, "load5": 0.5, "available_mb": 100, "pending": 0, "measured_at": 10_000}
    redis.set(headroom.key("box"), json.dumps(record))
    assert headroom.read(redis, "box", 40_000) == record
    assert headroom.read(redis, "box", 40_001) is None
    assert headroom.read(redis, "missing", 10_000) is None


def test_summary_names_each_number_or_missing_telemetry():
    record = {"cpus": 8, "load5": 3.456, "available_mb": 8000, "pending": 2, "measured_at": 1}
    assert headroom.summary(record) == "cpus=8\tload5=3.46\tavailable-mb=8000\tpending=2"
    assert headroom.summary(None) == "headroom=missing"


@pytest.mark.parametrize(
    ("node", "pod", "shown"),
    [(10356, 2576, 2576), (1900, 2576, 1900)],
)
def test_summary_shows_the_tighter_of_node_and_pod_memory_with_the_pod_limit(node, pod, shown):
    record = {
        "cpus": 16,
        "load5": 4.73,
        "available_mb": node,
        "total_mb": 29857,
        "pod_total_mb": 4200,
        "pod_available_mb": pod,
        "pending": 0,
        "measured_at": 1,
    }
    assert headroom.summary(record) == f"cpus=16\tload5=4.73\tavailable-mb={shown}\tpod-limit-mb=4200\tpending=0"


def test_summary_shows_node_memory_when_the_pod_reading_is_incomplete():
    record = {"cpus": 8, "load5": 1.0, "available_mb": 8000, "pod_total_mb": 4200, "pending": 0, "measured_at": 1}
    assert headroom.summary(record) == "cpus=8\tload5=1.00\tavailable-mb=8000\tpending=0"


def test_beat_publishes_headroom_at_the_beat_time_in_whole_milliseconds(redis, monkeypatch):
    calls = []
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon.time, "time", lambda: 12.3456)
    monkeypatch.setattr(headroom, "publish", lambda client, hive, now_ms: calls.append((client, hive, now_ms)))
    daemon.beat(redis, "machine")
    assert calls == [(redis, "machine", 12_345)]


def test_beat_leaves_headroom_with_its_heartbeat_when_a_later_step_fails(redis, monkeypatch):
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon.time, "time", lambda: 12.3456)
    monkeypatch.setattr(headroom, "measure", lambda pending, now_ms, proc, previous: {"measured_at": now_ms})

    def fails(*args):
        raise RuntimeError("herdr timed out")

    monkeypatch.setattr(daemon, "execution_report", fails)
    with pytest.raises(RuntimeError, match="^herdr timed out$"):
        daemon.beat(redis, "machine")
    assert redis.hget(registry.key("machine"), "heartbeat_at") == "12345"
    assert json.loads(redis.get(headroom.key("machine")))["measured_at"] == 12_345


def test_hive_list_shows_live_headroom_beside_each_hive(redis, monkeypatch, capsys):
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    monkeypatch.setattr(cli, "now_ms", lambda: 40_000)
    registry.update(redis, "box", ["ui=no"])
    registry.update(redis, "old", ["ui=no"])
    record = {"cpus": 8, "load5": 1.0, "available_mb": 512, "pending": 1, "measured_at": 20_000}
    redis.set(headroom.key("box"), json.dumps(record))
    redis.set(headroom.key("old"), json.dumps({**record, "measured_at": 9_999}))
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == (
        "box\tstale\tui=no\tmax-agents=20\texcluded=master\tcpus=8\tload5=1.00\tavailable-mb=512\tpending=1\n"
        "old\tstale\tui=no\tmax-agents=20\texcluded=master\theadroom=missing\n"
    )


POD_LIMITS = {
    "memory.max": "6606028800\n",
    "memory.current": "1048576000\n",
    "memory.stat": "anon 943718400\nfile 104857600\ninactive_file 104857600\n",
    "cpu.max": "250000 100000\n",
    "cpu.stat": "usage_usec 9000000\nuser_usec 6000000\n",
}


def limit(cgroup, files):
    for name, text in files.items():
        (cgroup / name).write_text(text)


def test_hive_list_shows_a_pod_hive_within_its_pod_memory_limit(redis, proc, cgroup, monkeypatch, capsys):
    limit(
        cgroup,
        {
            "memory.max": "4404019200\n",
            "memory.current": "2300563456\n",
            "memory.stat": "inactive_file 629370880\n",
        },
    )
    monkeypatch.setattr(headroom.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    monkeypatch.setattr(cli, "now_ms", lambda: 5_000)
    registry.update(redis, "anton-2", ["ui=no"])
    headroom.publish(redis, "anton-2", 5_000, proc)
    assert cli.main(["list"]) == 0
    assert "\tcpus=16\tload5=3.46\tavailable-mb=2606\tpod-limit-mb=4200\tpending=0\n" in capsys.readouterr().out


def test_measure_adds_the_pod_memory_and_cpu_limits_beside_the_node_reading(proc, cgroup, monkeypatch):
    monkeypatch.setattr(headroom.os, "cpu_count", lambda: 8)
    limit(cgroup, POD_LIMITS)
    assert headroom.measure(0, 5, proc) == {
        "cpus": 8,
        "load5": 3.456,
        "available_mb": 8000,
        "total_mb": 15625,
        "pod_total_mb": 6300,
        "pod_available_mb": 5400,
        "pod_cpus": 2.5,
        "pod_cpu_usec": 9_000_000,
        "pending": 0,
        "measured_at": 5,
    }


def test_measure_rounds_pod_memory_down_to_whole_megabytes(proc, cgroup):
    limit(cgroup, {"memory.max": "6607077375\n", "memory.current": "943718400\n", "memory.stat": "inactive_file 0\n"})
    measured = headroom.measure(0, 5, proc)
    assert (measured["pod_total_mb"], measured["pod_available_mb"]) == (6300, 5400)


@pytest.mark.parametrize(
    ("files", "pod"),
    [
        ({}, {}),
        ({"memory.max": "max\n", "memory.current": "100\n", "cpu.max": "max 100000\n"}, {}),
        (
            {"memory.max": "max\n", "cpu.max": "50000 100000\n", "cpu.stat": "usage_usec 7\n"},
            {"pod_cpus": 0.5, "pod_cpu_usec": 7},
        ),
        (
            {
                "memory.max": "2097152000\n",
                "memory.current": "0\n",
                "memory.stat": "inactive_file 0\n",
                "cpu.max": "max 1\n",
            },
            {"pod_total_mb": 2000, "pod_available_mb": 2000},
        ),
    ],
)
def test_measure_keeps_the_node_reading_alone_for_each_unlimited_pod_resource(proc, cgroup, files, pod):
    limit(cgroup, files)
    measured = headroom.measure(0, 5, proc)
    assert {field: value for field, value in measured.items() if field.startswith("pod_")} == pod
    assert measured["total_mb"] == 15625


@pytest.mark.parametrize(
    "files",
    [
        {"memory.max": "6606028800\n"},
        {"memory.max": "6606028800\n", "memory.current": "lots\n"},
        {"memory.max": "6606028800\n", "memory.current": "0\n"},
        {"memory.max": "6606028800\n", "memory.current": "0\n", "memory.stat": "anon 0\n"},
        {"memory.max": "\n", "memory.current": "0\n"},
        {"cpu.max": "250000\n"},
        {"cpu.max": "many 100000\n"},
        {"cpu.max": "250000 100000\n"},
        {"cpu.max": "250000 100000\n", "cpu.stat": "usage_usec\n"},
    ],
)
def test_measure_is_none_on_an_unreadable_pod_limit(proc, cgroup, files):
    limit(cgroup, files)
    assert headroom.measure(0, 5, proc) is None


@pytest.mark.parametrize(
    ("previous", "load"),
    [
        ({"pod_cpu_usec": 6_000_000, "measured_at": 5_000}, {"pod_load": 0.2}),
        ({"pod_cpu_usec": 9_000_000, "measured_at": 5_000}, {"pod_load": 0.0}),
        ({"pod_cpu_usec": 8_999_000, "measured_at": 19_999}, {"pod_load": 1.0}),
        (None, {}),
        ({"measured_at": 5_000}, {}),
        ({"pod_cpu_usec": 9_000_001, "measured_at": 5_000}, {}),
        ({"pod_cpu_usec": 6_000_000, "measured_at": 20_000}, {}),
        ({"pod_cpu_usec": 6_000_000, "measured_at": 20_001}, {}),
    ],
)
def test_measure_reads_pod_load_from_cpu_time_since_the_previous_reading(proc, cgroup, previous, load):
    limit(cgroup, POD_LIMITS)
    measured = headroom.measure(0, 20_000, proc, previous)
    assert {field: value for field, value in measured.items() if field == "pod_load"} == load


def test_publish_reads_pod_load_against_the_previous_live_reading(redis, proc, cgroup):
    limit(cgroup, POD_LIMITS)
    headroom.publish(redis, "box", 5_000, proc)
    assert "pod_load" not in json.loads(redis.get(headroom.key("box")))
    (cgroup / "cpu.stat").write_text("usage_usec 12000000\n")
    headroom.publish(redis, "box", 20_000, proc)
    assert json.loads(redis.get(headroom.key("box")))["pod_load"] == 0.2


def test_the_cgroup_folder_defaults_to_the_cgroup_v2_mount():
    assert DEFAULT_CGROUP == Path("/sys/fs/cgroup")
