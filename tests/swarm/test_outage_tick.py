import pytest
from redis.exceptions import ConnectionError

from scripts.swarm import cli, outage
from scripts.swarm.store import SwarmError
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit


def test_scheduled_tick_exits_cleanly_when_redis_connect_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    (tmp_path / "sw").mkdir()
    monkeypatch.setattr(cli, "now_ms", lambda: 1000)

    def unavailable():
        raise SwarmError("Redis is unreachable")

    monkeypatch.setattr(cli, "connect", unavailable)
    assert cli.main(["tick"]) == 0
    record = outage.read("sw")["redis"]
    assert record["started_at"] == 1000
    assert record["ended_at"] is None
    assert "Redis connect check failed" in record["evidence"][0]
    assert "Redis connect check failed" in capsys.readouterr().out


def test_a_tick_records_redis_failure_and_recovery_before_its_work(store, tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    monkeypatch.setattr(cli, "now_ms", lambda: 1000)

    with monkeypatch.context() as broken:

        def unavailable():
            raise ConnectionError("unreachable")

        broken.setattr(store.redis, "ping", unavailable)
        assert cli.run_tick(store, "sw", tasks(), FakeRuntime()) == ["Redis ping check failed: ConnectionError"]
    assert outage.read("sw")["redis"]["started_at"] == 1000
    monkeypatch.setattr(cli, "now_ms", lambda: 2000)
    cli.run_tick(store, "sw", tasks(), FakeRuntime())
    assert outage.read("sw")["redis"]["ended_at"] == 2000


def test_a_redis_failure_during_a_tick_is_recorded_without_raising(store, tmp_path, monkeypatch):
    from scripts.swarm import lease

    monkeypatch.setattr(outage, "HOME", tmp_path)

    def unavailable(*args):
        raise ConnectionError("lost connection")

    monkeypatch.setattr(lease, "acquire", unavailable)
    assert cli.run_tick(store, "sw", tasks(), FakeRuntime()) == ["Redis operation failed: ConnectionError"]
    assert outage.open_records("sw")[0]["kind"] == "redis"


def test_scheduled_tick_exits_cleanly_if_redis_drops_before_swarm_discovery(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    (tmp_path / "sw").mkdir()
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: None)

    def unavailable():
        raise ConnectionError("lost connection during discovery")

    monkeypatch.setattr(store, "slugs", unavailable)
    assert cli.main(["tick"]) == 0
    assert outage.open_records("sw")[0]["kind"] == "redis"
    assert "Redis operation failed: ConnectionError" in capsys.readouterr().out
