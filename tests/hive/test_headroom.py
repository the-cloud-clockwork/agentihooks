import json

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


def test_measure_reads_the_five_minute_load_and_available_memory(proc, monkeypatch):
    monkeypatch.setattr(headroom.os, "cpu_count", lambda: 8)
    assert headroom.measure(3, 77_000, proc) == {
        "cpus": 8,
        "load5": 3.456,
        "available_mb": 8000,
        "pending": 3,
        "measured_at": 77_000,
    }


def test_measure_counts_one_cpu_when_the_count_is_unknown(proc, monkeypatch):
    monkeypatch.setattr(headroom.os, "cpu_count", lambda: None)
    assert headroom.measure(0, 1, proc)["cpus"] == 1


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
        "pending": 2,
        "measured_at": 5_000,
    }
    assert redis.pttl(headroom.key("box")) == 30_000


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
