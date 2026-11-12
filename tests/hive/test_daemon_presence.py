from typing import Any

from hooks.observability import presence
from scripts.hive import daemon


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
