from types import SimpleNamespace
from typing import Any

from hooks.observability import presence
from scripts.hive import daemon

REDIS: Any = object()


def emitted(monkeypatch, shown):
    calls = []
    redis: Any = object()

    def show(client, hive):
        assert client is redis and hive == "hive-9"
        return shown

    monkeypatch.setattr(daemon.registry, "show", show)
    monkeypatch.setattr(daemon.account_sessions, "local_processes", lambda: {"claude": {1: "a", 2: "b"}, "codex": {}})
    monkeypatch.setattr(daemon.updater, "installed_version", lambda: "7.7.7")
    monkeypatch.setattr(presence, "hive", lambda *args: calls.append(args))
    daemon.emit_presence(redis, "hive-9", 12_500)
    return calls


def test_a_headed_hive_emits_its_registered_name_release_and_agents(monkeypatch):
    calls = emitted(monkeypatch, {"name": "automation", "ui": "yes"})
    assert calls == [("hive-9", "automation", True, "7.7.7", {"claude": 2, "codex": 0}, 12.5)]


def test_an_unregistered_hive_emits_under_its_id_as_headless(monkeypatch):
    calls = emitted(monkeypatch, None)
    assert calls == [("hive-9", "hive-9", False, "7.7.7", {"claude": 2, "codex": 0}, 12.5)]


def test_the_headroom_thread_emits_presence_after_each_reading(monkeypatch):
    order = []
    stop = daemon.threading.Event()
    monkeypatch.setattr(daemon, "scheduled_wait", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon, "timer_state", lambda waiter: None)
    monkeypatch.setattr(daemon, "time", SimpleNamespace(time=lambda: 3.0))

    from scripts.hive import headroom, orders

    monkeypatch.setattr(headroom, "publish", lambda redis, hive, now: order.append(("headroom", hive, now)))
    monkeypatch.setattr(daemon, "emit_presence", lambda redis, hive, now: order.append(("presence", hive, now)))
    monkeypatch.setattr(daemon.registry, "touch", lambda redis, hive, now: stop.set())
    monkeypatch.setattr(orders, "alive", lambda redis, hive: None)
    monkeypatch.setattr(daemon, "refresh_ready", lambda: None)
    daemon.publish_headroom(REDIS, REDIS, "hive-9", stop)
    assert order == [("headroom", "hive-9", 3000), ("presence", "hive-9", 3000)]


def test_a_failed_presence_emit_is_logged_after_the_liveness_touch(monkeypatch, capsys):
    touched = []
    stop = daemon.threading.Event()
    monkeypatch.setattr(daemon, "scheduled_wait", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon, "timer_state", lambda waiter: None)
    monkeypatch.setattr(daemon, "time", SimpleNamespace(time=lambda: 3.0))

    from scripts.hive import headroom, orders

    def fail(redis, hive, now):
        stop.set()
        raise OSError("proc gone")

    monkeypatch.setattr(headroom, "publish", lambda redis, hive, now: None)
    monkeypatch.setattr(daemon, "emit_presence", fail)
    monkeypatch.setattr(daemon.registry, "touch", lambda redis, hive, now: touched.append(hive))
    monkeypatch.setattr(orders, "alive", lambda redis, hive: None)
    monkeypatch.setattr(daemon, "refresh_ready", lambda: None)
    daemon.publish_headroom(REDIS, REDIS, "hive-9", stop)
    assert touched == ["hive-9"]
    assert capsys.readouterr().err == "hive presence emit failed (OSError: proc gone)\n"
