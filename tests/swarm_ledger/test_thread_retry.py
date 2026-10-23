import threading
from unittest.mock import Mock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from scripts.scheduler import process_timer
from scripts.swarm_ledger import ledger_server

pytestmark = pytest.mark.unit

UNRESOLVED = RedisConnectionError(
    "Error -3 connecting to swarm-internal.homeofanton.com:6379. Temporary failure in name resolution."
)


class Stop(BaseException):
    pass


def test_the_follow_thread_recovers_after_the_first_redis_connect_fails_on_name_resolution(monkeypatch, capsys):
    waits, slept = [], []

    def wait(interval):
        waits.append(interval)
        if len(waits) == 1:
            raise UNRESOLVED
        raise Stop

    monkeypatch.setattr(ledger_server, "SCHEDULER_UNAVAILABLE", threading.Event())
    monkeypatch.setattr(ledger_server.HUB, "watched", lambda: [])
    monkeypatch.setattr(process_timer, "wait", wait)
    with pytest.raises(Stop):
        ledger_server.retrying(ledger_server.follow_shared, sleep=slept.append)()
    assert waits == [ledger_server.FOLLOW_S, ledger_server.FOLLOW_S]
    assert slept == [1.0]
    assert capsys.readouterr().err == (
        "follow_shared stopped, retrying in 1s: Error -3 connecting to swarm-internal.homeofanton.com:6379."
        " Temporary failure in name resolution.\n"
    )


def test_the_retry_backoff_doubles_up_to_thirty_seconds():
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 7:
            raise Stop

    def fails():
        raise UNRESOLVED

    with pytest.raises(Stop):
        ledger_server.retrying(fails, sleep=sleep)()
    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_the_runner_sleeps_by_default_between_attempts():
    runs = []

    def fails_once():
        runs.append(1)
        if len(runs) == 1:
            raise UNRESOLVED
        raise Stop

    with pytest.raises(Stop):
        ledger_server.retrying(fails_once, first=0.01)()
    assert runs == [1, 1]


def test_a_loop_that_returns_runs_again_without_waiting():
    runs = []

    def returns():
        runs.append(1)
        if len(runs) == 3:
            raise Stop

    with pytest.raises(Stop):
        ledger_server.retrying(returns, sleep=lambda seconds: pytest.fail(f"slept {seconds}"))()
    assert runs == [1, 1, 1]


def test_the_shared_server_runs_watch_and_follow_under_the_retrying_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger_server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(ledger_server.storage, "shared", lambda: True)
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Mock())
    monkeypatch.setattr(ledger_server.threading, "Thread", Mock())
    ledger_server.serve()
    targets = [call.kwargs["target"] for call in ledger_server.threading.Thread.call_args_list]
    wrapped = [target.__wrapped__ for target in targets if hasattr(target, "__wrapped__")]
    assert wrapped == [ledger_server.watch_ledgers, ledger_server.follow_shared]
