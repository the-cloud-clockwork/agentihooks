import threading
from pathlib import Path

import pytest

from scripts.scheduler import process_timer, service
from scripts.swarm_ledger import ledger_server

pytestmark = pytest.mark.unit


class Stop(Exception):
    pass


@pytest.fixture
def unwritable_cache(monkeypatch):
    import fakeredis

    (Path.home() / ".cache").write_text("")
    monkeypatch.setattr(
        service.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("scheduler supervisor started")
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    for name in ("AGENTIHOOKS_DEPLOYMENT", "AGENTIHOOKS_HIVE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(process_timer, "blocking_client", lambda: fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(ledger_server, "SCHEDULER_UNAVAILABLE", threading.Event())


def test_the_watch_loop_keeps_passing_when_the_scheduler_lock_folder_cannot_be_created(
    unwritable_cache, monkeypatch, capsys
):
    passes, slept = [], []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            raise Stop

    for name in ("bin_closed_without_swarm", "expire_alerts"):
        monkeypatch.setattr(ledger_server, name, lambda: None)
    monkeypatch.setattr(ledger_server, "sample_streams", lambda: passes.append(1))
    monkeypatch.setattr(ledger_server.ledger_bin, "tidy", lambda: None)
    monkeypatch.setattr(ledger_server, "reloading", lambda *args: False)
    monkeypatch.setattr(ledger_server, "unrelayed", lambda: {})
    monkeypatch.setattr(ledger_server, "metrics_journal", lambda *args: None)
    monkeypatch.setattr(ledger_server.time, "sleep", sleep)
    with pytest.raises(Stop):
        ledger_server.watch_ledgers()
    assert passes == [1, 1, 1]
    assert slept == [2.0, 2.0, 2.0]
    assert capsys.readouterr().err.count("scheduler wait unavailable, sleeping instead") == 1


def test_a_working_scheduler_wait_is_the_only_wait(monkeypatch):
    waited = []
    monkeypatch.setattr(ledger_server, "SCHEDULER_UNAVAILABLE", threading.Event())
    monkeypatch.setattr(process_timer, "wait", waited.append)
    monkeypatch.setattr(ledger_server.time, "sleep", lambda seconds: pytest.fail(f"slept {seconds}"))
    ledger_server.scheduled_wait(2.0)
    assert waited == [2.0]
    assert not ledger_server.SCHEDULER_UNAVAILABLE.is_set()


def test_a_scheduler_wait_refused_by_redis_sleeps_instead(monkeypatch, capsys):
    from redis.exceptions import NoPermissionError

    refusals, slept = [], []

    def refused(interval):
        refusals.append(interval)
        raise NoPermissionError("No permissions to access a key")

    monkeypatch.setattr(ledger_server, "SCHEDULER_UNAVAILABLE", threading.Event())
    monkeypatch.setattr(process_timer, "wait", refused)
    monkeypatch.setattr(ledger_server.time, "sleep", slept.append)
    ledger_server.scheduled_wait(2.0)
    ledger_server.scheduled_wait(2.0)
    assert refusals == [2.0]
    assert slept == [2.0, 2.0]
    assert ledger_server.SCHEDULER_UNAVAILABLE.is_set()
    assert capsys.readouterr().err.count("scheduler wait unavailable, sleeping instead: No permissions") == 1


def test_a_lost_redis_connection_still_reaches_the_watch_loop(monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    def lost(_interval):
        raise RedisConnectionError("Connection reset by peer")

    monkeypatch.setattr(ledger_server, "SCHEDULER_UNAVAILABLE", threading.Event())
    monkeypatch.setattr(process_timer, "wait", lost)
    monkeypatch.setattr(ledger_server.time, "sleep", lambda seconds: pytest.fail(f"slept {seconds}"))
    with pytest.raises(RedisConnectionError, match="Connection reset"):
        ledger_server.scheduled_wait(2.0)
    assert not ledger_server.SCHEDULER_UNAVAILABLE.is_set()


def test_a_scheduler_failure_other_than_the_filesystem_still_reaches_the_watch_loop(monkeypatch):
    def broken(_interval):
        raise RuntimeError("scheduler bug")

    monkeypatch.setattr(ledger_server, "SCHEDULER_UNAVAILABLE", threading.Event())
    monkeypatch.setattr(process_timer, "wait", broken)
    monkeypatch.setattr(ledger_server.time, "sleep", lambda seconds: pytest.fail(f"slept {seconds}"))
    with pytest.raises(RuntimeError, match="scheduler bug"):
        ledger_server.scheduled_wait(2.0)
    assert not ledger_server.SCHEDULER_UNAVAILABLE.is_set()
