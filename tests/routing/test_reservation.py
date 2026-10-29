from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.routing import reservation
from scripts.routing.master_account import MasterAccount
from scripts.swarm import store as swarm_store


@pytest.mark.parametrize(
    ("masters", "expected"),
    [(0, None), (-1, None), (1, 70.0), (2, 60.0), (3, 50.0), (4, 50.0), (9, 50.0)],
)
def test_ceiling_steps_with_live_masters(masters, expected):
    assert reservation.ceiling(masters) == expected


@pytest.mark.parametrize(
    ("week_used", "masters", "expected"),
    [
        (69.9, 1, False),
        (70.0, 1, True),
        (70.1, 1, True),
        (59.9, 2, False),
        (60.0, 2, True),
        (49.9, 3, False),
        (50.0, 3, True),
        (99.0, 0, False),
        (None, 1, False),
        (0.0, 1, False),
    ],
)
def test_reached_at_the_ceiling(week_used, masters, expected):
    assert reservation.reached(week_used, masters) is expected


def test_lowest_ceiling_is_the_three_master_step():
    assert reservation.LOWEST == 50.0


def test_reason_names_the_reservation():
    assert reservation.reason(1) == "reserved for 1 live master: workers stop at 70% of the week"
    assert reservation.reason(2) == "reserved for 2 live masters: workers stop at 60% of the week"
    assert reservation.reason(0) == ""


@dataclass
class _Agent:
    lane: str
    harness: str
    account: str
    state: str = "working"
    route_kind: str = "subscription"


class _Store:
    def __init__(self, swarms):
        self.swarms = swarms

    def slugs(self):
        return sorted(self.swarms)

    def agents(self, slug):
        return self.swarms[slug]


def _no_store(environ):
    return None


def test_two_swarms_masters_sum():
    store = _Store(
        {
            "one": [_Agent("master", "claude", "main"), _Agent("eng", "claude", "main")],
            "two": [
                _Agent("master", "claude", "main"),
                _Agent("master", "claude", "other"),
                _Agent("master", "codex", "main"),
                _Agent("master", "claude", "main", state="finished"),
                _Agent("master", "claude", "main", route_kind="interactive"),
                _Agent("master", "claude", "main", route_kind="master-login"),
            ],
        }
    )
    assert reservation.master_agents(store, "claude", "main") == 2
    assert reservation.master_agents(store, "codex", "main") == 1
    assert reservation.master_agents(None, "claude", "main") == 0


def _process(proc: Path, pid: int, argv: list[str], env: dict[str, str], comm: str) -> None:
    root = proc / str(pid)
    root.mkdir(parents=True)
    (root / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
    (root / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
    (root / "comm").write_text(comm + "\n")
    (root / "stat").write_text(f"{pid} ({comm}) S 1 1 1 0 -1\n")


def test_interactive_sessions_count_the_slug_marker(tmp_path):
    _process(tmp_path, 10, ["claude"], {"AH_ROUTE_INTERACTIVE_main": "1"}, "claude")
    _process(tmp_path, 11, ["claude"], {"AH_ROUTE_INTERACTIVE_main": "1"}, "claude")
    _process(tmp_path, 12, ["claude"], {"AH_CC_TOKEN_main": "x"}, "claude")
    _process(tmp_path, 13, ["claude"], {"AH_ROUTE_INTERACTIVE_other": "1"}, "claude")
    _process(tmp_path, 14, ["claude", "-p"], {"AH_ROUTE_INTERACTIVE_main": "1"}, "claude")
    _process(tmp_path, 15, ["codex"], {"AH_ROUTE_INTERACTIVE_default": "1"}, "codex")
    assert reservation.interactive_sessions("claude", "main", tmp_path) == 2
    assert reservation.interactive_sessions("codex", "default", tmp_path) == 1
    assert reservation.interactive_sessions("claude", "default", tmp_path) == 0
    assert reservation.interactive_sessions("claude", "main", tmp_path / "missing") == 0


def test_masters_live_sums_sessions_and_agents(tmp_path):
    _process(tmp_path, 10, ["claude"], {"AH_ROUTE_INTERACTIVE_main": "1"}, "claude")
    store = _Store({"one": [_Agent("master", "claude", "main")], "two": [_Agent("master", "claude", "main")]})
    assert reservation.masters_live("claude", "main", store, tmp_path) == 3


def test_current_reads_the_declared_account(tmp_path, monkeypatch):
    _process(tmp_path, 10, ["claude"], {"AH_ROUTE_INTERACTIVE_main": "1"}, "claude")
    declared = {"claude": MasterAccount("claude", "main")}
    monkeypatch.setattr(reservation.master_account, "load", lambda environ: declared)
    held = reservation.current("claude", {}, store_of=_no_store, proc=tmp_path)
    assert held == reservation.Reservation("claude", "main", 1)
    assert held.ceiling == 70.0
    assert held.closes("main", 70.0)
    assert not held.closes("main", 69.9)
    assert not held.closes("other", 99.0)
    assert held.reason == "reserved for 1 live master: workers stop at 70% of the week"
    assert reservation.current("codex", {}, store_of=_no_store, proc=tmp_path) is None


def test_current_without_live_masters_holds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(reservation.master_account, "load", lambda environ: {"claude": MasterAccount("claude", "main")})
    held = reservation.current("claude", {}, store_of=_no_store, proc=tmp_path)
    assert held == reservation.Reservation("claude", "main", 0)
    assert held.ceiling is None
    assert not held.closes("main", 99.0)
    assert held.reason == ""


def test_current_survives_an_unreadable_declaration(tmp_path, monkeypatch):
    def broken(environ):
        raise OSError("redis down")

    monkeypatch.setattr(reservation.master_account, "load", broken)
    assert reservation.current("claude", {}, store_of=_no_store, proc=tmp_path) is None


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"AH_ROUTE_INTERACTIVE_main": "1"}, True),
        ({"AGENTIHOOKS_AGENT_NAME": "master@323133-0108"}, True),
        ({"AGENTIHOOKS_AGENT_NAME": "engineer@323133-0108"}, False),
        ({"AH_CC_TOKEN_main": "x"}, False),
        ({}, False),
    ],
)
def test_is_master_session(environ, expected):
    assert reservation.is_master_session(environ) is expected


def test_current_opens_the_swarm_store_by_default(tmp_path, monkeypatch):
    store = _Store({"one": [_Agent("master", "claude", "main")]})
    monkeypatch.setattr(reservation.master_account, "load", lambda environ: {"claude": MasterAccount("claude", "main")})
    monkeypatch.setattr(reservation, "_store", lambda environ: store)
    assert reservation.current("claude", {}, proc=tmp_path) == reservation.Reservation("claude", "main", 1)


def test_current_survives_a_redis_failure(tmp_path, monkeypatch):
    from redis.exceptions import ConnectionError

    def broken(environ):
        raise ConnectionError("refused")

    monkeypatch.setattr(reservation.master_account, "load", broken)
    assert reservation.current("claude", {}, store_of=_no_store, proc=tmp_path) is None


def test_store_wraps_the_swarm_client(monkeypatch):
    from redis.exceptions import ConnectionError

    def refused(environ):
        raise ConnectionError("refused")

    monkeypatch.setattr(swarm_store, "redis_client", refused)
    assert reservation._store({}) is None
    client = object()
    environ = {"AGENTIHOOKS_SWARM_REDIS_URL": "redis://swarm"}
    monkeypatch.setattr(swarm_store, "redis_client", lambda given: client if given == environ else None)
    assert reservation._store(environ).redis is client


def test_current_reads_masters_through_the_given_store(tmp_path, monkeypatch):
    store = _Store({"one": [_Agent("master", "claude", "main")]})
    environ = {"AGENTIHOOKS_SWARM_REDIS_URL": "redis://swarm"}
    monkeypatch.setattr(reservation.master_account, "load", lambda environ: {"claude": MasterAccount("claude", "main")})
    held = reservation.current(
        "claude", environ, store_of=lambda given: store if given == environ else None, proc=tmp_path
    )
    assert held == reservation.Reservation("claude", "main", 1)


def test_current_survives_a_store_that_raises(tmp_path, monkeypatch):
    from redis.exceptions import ConnectionError

    def broken(environ):
        raise ConnectionError("refused")

    monkeypatch.setattr(reservation.master_account, "load", lambda environ: {"claude": MasterAccount("claude", "main")})
    assert reservation.current("claude", {}, store_of=broken, proc=tmp_path) is None


def test_reason_at_three_masters_names_fifty():
    assert reservation.reason(3) == "reserved for 3 live masters: workers stop at 50% of the week"


def test_interactive_sessions_skip_non_process_entries(tmp_path):
    _process(tmp_path, 10, ["claude"], {"AH_ROUTE_INTERACTIVE_main": "1"}, "claude")
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "environ").write_bytes(b"AH_ROUTE_INTERACTIVE_main=1\0")
    assert reservation.interactive_sessions("claude", "main", tmp_path) == 1


@pytest.mark.parametrize(
    "environ",
    [
        {"AGENTIHOOKS_AGENT_NAME": "master-x"},
        {"AGENTIHOOKS_AGENT_NAME": "masterful@1"},
        {"X_AH_ROUTE_INTERACTIVE_main": "1"},
    ],
)
def test_names_near_the_master_markers_are_workers(environ):
    assert reservation.is_master_session(environ) is False


def test_the_real_process_table_is_the_default(monkeypatch):
    monkeypatch.setattr(
        reservation.master_account, "load", lambda environ: {"claude": MasterAccount("claude", "absent-slug")}
    )
    assert reservation.interactive_sessions("claude", "absent-slug") == 0
    assert reservation.masters_live("claude", "absent-slug") == 0
    assert reservation.current("claude", {}, store_of=_no_store) == reservation.Reservation("claude", "absent-slug", 0)


def test_only_workers_are_held_by_the_reservation(monkeypatch):
    monkeypatch.setattr(
        reservation,
        "current",
        lambda harness, environ: reservation.Reservation(harness, environ["AGENTIHOOKS_AGENT_NAME"], 1),
    )
    assert reservation.for_worker("claude", {"AGENTIHOOKS_AGENT_NAME": "master@1"}) is None
    assert reservation.for_worker("codex", {"AGENTIHOOKS_AGENT_NAME": "engineer@1"}) == reservation.Reservation(
        "codex", "engineer@1", 1
    )


def test_launch_slot_reading_keeps_the_exact_decision_arguments(monkeypatch):
    from unittest.mock import Mock

    from scripts.hive import registry
    from scripts.swarm.keyspace import ROOT

    redis = Mock()
    redis.eval.return_value = [0, 7]
    monkeypatch.setattr(registry, "hives", lambda client: [{"id": "worker", "heartbeat_at": 123}])
    reading = reservation.LaunchSlots(redis).reserve_reading("codex:one", "next", 4, 1000, "old")
    assert reading == reservation.SlotReading(False, 7)
    redis.eval.assert_called_once_with(
        reservation._LAUNCH,
        3,
        f"{ROOT}:launch-slots",
        f"{ROOT}:launch-slot:next",
        registry.INDEX,
        "reserve",
        "codex:one",
        "next",
        4,
        1000,
        ROOT,
        "old",
        '{"worker": 123}',
    )


def test_codex_masters_are_counted_by_the_codex_marker(tmp_path):
    _process(tmp_path, 15, ["codex"], {"AH_ROUTE_INTERACTIVE_default": "1"}, "codex")
    assert reservation.masters_live("codex", "default", None, tmp_path) == 1


def test_store_survives_an_unreachable_socket(monkeypatch):
    def unreachable(environ):
        raise OSError("no route")

    monkeypatch.setattr(swarm_store, "redis_client", unreachable)
    assert reservation._store({}) is None


def test_an_interactive_master_counts_once(tmp_path):
    _process(tmp_path, 10, ["claude"], {"AH_ROUTE_INTERACTIVE_main": "1"}, "claude")
    store = _Store({"one": [_Agent("master", "claude", "main", route_kind="interactive")]})
    assert reservation.masters_live("claude", "main", store, tmp_path) == 1


def test_a_reservation_closes_only_a_subscription_row_of_its_harness_and_slug_past_the_ceiling():
    held = reservation.Reservation("codex", "default", 1)
    assert held.closes_row("codex", "subscription", "default", 30.0)
    assert [
        held.closes_row("codex", "subscription", "default", 30.1),
        held.closes_row("claude", "subscription", "default", 10.0),
        held.closes_row("codex", "api", "default", 10.0),
        held.closes_row("codex", "subscription", "spare", 10.0),
        held.closes_row("codex", "subscription", "default", None),
        reservation.Reservation("codex", "default", 0).closes_row("codex", "subscription", "default", 10.0),
    ] == [False] * 6
