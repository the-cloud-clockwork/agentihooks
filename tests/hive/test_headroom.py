import json
from pathlib import Path

import pytest

from scripts.hive import cli, daemon, headroom, registry
from scripts.hive.orders import Orders

pytestmark = pytest.mark.unit

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
        "measured_at": 77_000,
    }


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


def test_beat_publishes_headroom_at_the_beat_time_in_whole_milliseconds(redis, monkeypatch):
    calls = []
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon.time, "time", lambda: 12.3456)
    monkeypatch.setattr(headroom, "publish", lambda client, hive, now_ms: calls.append((client, hive, now_ms)))
    daemon.beat(redis, "machine")
    assert calls == [(redis, "machine", 12_345)]


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


POD_LIMITS = {"memory.max": "6606028800\n", "memory.current": "1048576000\n", "cpu.max": "250000 100000\n"}


def test_measure_adds_the_pod_memory_and_cpu_limits_beside_the_node_reading(proc, cgroup, monkeypatch):
    monkeypatch.setattr(headroom.os, "cpu_count", lambda: 8)
    for name, text in POD_LIMITS.items():
        (cgroup / name).write_text(text)
    assert headroom.measure(0, 5, proc) == {
        "cpus": 8,
        "load5": 3.456,
        "available_mb": 8000,
        "total_mb": 15625,
        "pod_total_mb": 6300,
        "pod_available_mb": 5300,
        "pod_cpus": 2.5,
        "pending": 0,
        "measured_at": 5,
    }


def test_measure_rounds_pod_memory_down_to_whole_megabytes(proc, cgroup):
    (cgroup / "memory.max").write_text("6606029823\n")
    (cgroup / "memory.current").write_text("1048576001\n")
    measured = headroom.measure(0, 5, proc)
    assert (measured["pod_total_mb"], measured["pod_available_mb"]) == (6300, 5299)


@pytest.mark.parametrize(
    ("files", "pod"),
    [
        ({}, {}),
        ({"memory.max": "max\n", "memory.current": "100\n", "cpu.max": "max 100000\n"}, {}),
        ({"memory.max": "max\n", "cpu.max": "50000 100000\n"}, {"pod_cpus": 0.5}),
        (
            {"memory.max": "2097152000\n", "memory.current": "0\n", "cpu.max": "max 100000\n"},
            {"pod_total_mb": 2000, "pod_available_mb": 2000},
        ),
    ],
)
def test_measure_keeps_the_node_reading_alone_for_each_unlimited_pod_resource(proc, cgroup, files, pod):
    for name, text in files.items():
        (cgroup / name).write_text(text)
    measured = headroom.measure(0, 5, proc)
    assert {field: value for field, value in measured.items() if field.startswith("pod_")} == pod
    assert measured["total_mb"] == 15625


@pytest.mark.parametrize(
    "files",
    [
        {"memory.max": "6606028800\n"},
        {"memory.max": "6606028800\n", "memory.current": "lots\n"},
        {"memory.max": "\n", "memory.current": "0\n"},
        {"cpu.max": "250000\n"},
        {"cpu.max": "many 100000\n"},
    ],
)
def test_measure_is_none_on_an_unreadable_pod_limit(proc, cgroup, files):
    for name, text in files.items():
        (cgroup / name).write_text(text)
    assert headroom.measure(0, 5, proc) is None


def test_measure_reads_the_cgroup_folder_of_its_own_process_by_default():
    assert headroom.CGROUP == Path("/sys/fs/cgroup")
