"""The account session readers reach their store families only through the storage protocol."""

import ast
from pathlib import Path

import pytest

from hooks.context import account_sessions as acc
from scripts.events.bus import StateConflict

SOURCE = Path(acc.__file__)
REDIS_NAMES = {"redis", "hooks._redis", "get_redis", "RedisStore", "redis_client", "RedisError"}


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
    return names


def _conflict():
    raise StateConflict("store moved")


def _redis_down():
    from redis.exceptions import ConnectionError

    raise ConnectionError("down")


def _bus_down():
    from nats.errors import Error

    raise Error("down")


def _hive_failure(raised):
    def held(harness, now, own):
        raised()

    return held


def test_the_readers_import_no_redis_client():
    imported = _imported_names(ast.parse(SOURCE.read_text()))

    assert {name for name in imported if name.split(".")[0] == "redis"} == set()
    assert imported & REDIS_NAMES == set()


def test_managed_held_reads_transport_records_from_the_binding_family(monkeypatch):
    monkeypatch.setattr(acc, "_LAST_HELD", set())
    monkeypatch.setattr("scripts.codex_session.binding.transport_records", lambda: ["exe-a1", "exe-b2"])

    assert acc.managed_held() == {("exe-a1", "default"), ("exe-b2", "default")}


def test_managed_held_holds_nothing_without_a_store(monkeypatch):
    monkeypatch.setattr(acc, "_LAST_HELD", {("exe-old", "alpha")})
    monkeypatch.setattr("scripts.codex_session.binding.transport_records", lambda: None)

    assert acc.managed_held() == set()


def test_managed_held_keeps_what_it_last_held_on_a_protocol_failure(monkeypatch):
    monkeypatch.setattr(acc, "_LAST_HELD", {("exe-old", "alpha")})
    monkeypatch.setattr("scripts.codex_session.binding.transport_records", _conflict)

    assert acc.managed_held() == {("exe-old", "alpha")}


def test_fleet_held_reads_the_account_family(monkeypatch):
    monkeypatch.setattr("scripts.swarm_v2.accounts.fleet_held_now", lambda: {"alpha": 2})

    assert acc.fleet_held({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}) == {"alpha": 2}


def test_fleet_held_holds_nothing_on_a_protocol_failure(monkeypatch):
    monkeypatch.setattr("scripts.swarm_v2.accounts.fleet_held_now", _conflict)

    assert acc.fleet_held({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}) == {}


def test_hive_held_reads_the_launch_reservation_family(monkeypatch):
    seen = []

    def held(harness, now, own):
        seen.append((harness, now, own))
        return {"alpha": 3}

    monkeypatch.setattr("time.time", lambda: 42.5)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "hive-1")
    monkeypatch.setattr("scripts.routing.aggregate.held_elsewhere", held)

    assert acc.hive_held("codex") == {"alpha": 3}
    assert seen == [("codex", 42.5, "hive-1")]


def test_hive_held_holds_nothing_on_a_protocol_failure(monkeypatch):
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "hive-1")
    monkeypatch.setattr("scripts.routing.aggregate.held_elsewhere", _hive_failure(_conflict))

    assert acc.hive_held("claude") == {}


@pytest.mark.parametrize("raised", [_redis_down, _bus_down])
def test_the_readers_survive_redis_and_bus_failures_on_the_bus_backend(monkeypatch, raised):
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setattr(acc, "_LAST_HELD", {("exe-old", "alpha")})
    monkeypatch.setattr("scripts.codex_session.binding.transport_records", raised)
    monkeypatch.setattr("scripts.swarm_v2.accounts.fleet_held_now", raised)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "hive-1")
    monkeypatch.setattr("scripts.routing.aggregate.held_elsewhere", _hive_failure(raised))

    assert acc.managed_held() == {("exe-old", "alpha")}
    assert acc.fleet_held({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}) == {}
    assert acc.hive_held("claude") == {}


def test_the_readers_survive_redis_failures_on_an_unknown_backend(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "carrier-pigeon")
    monkeypatch.setattr(acc, "_LAST_HELD", {("exe-old", "alpha")})
    monkeypatch.setattr("scripts.codex_session.binding.transport_records", _redis_down)
    monkeypatch.setattr("scripts.swarm_v2.accounts.fleet_held_now", _redis_down)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "hive-1")
    monkeypatch.setattr("scripts.routing.aggregate.held_elsewhere", _hive_failure(_redis_down))

    assert acc.managed_held() == {("exe-old", "alpha")}
    assert acc.fleet_held({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}) == {}
    assert acc.hive_held("claude") == {}
