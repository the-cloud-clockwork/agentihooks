"""The controller reads hive quota once per tick pass and closes an account a hive reading marks refused."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from scripts import claude_quota_balancer as balancer
from scripts.hive import daemon
from scripts.routing import aggregate
from scripts.swarm import capacity
from scripts.swarm.store import RedisStore
from scripts.tick import pool

ENVIRON = {"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}


def _quota(**extra: object) -> dict:
    return {
        "observed_at": 100,
        "five_hour": {"used": 4, "resets_at": 500},
        "seven_day": {"used": 2, "resets_at": 600},
        **extra,
    }


def _records(**extra: object) -> dict[str, dict]:
    return {
        f"claude:{name}": {
            "harness": "claude",
            "account": name,
            "kind": "pool",
            "sessions": 1,
            "quota": _quota(**extra),
        }
        for name in ("one", "two")
    }


class _Store:
    redis = object()


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Count hive report scans, with a declared Claude master so the reservation reads the hive rows too."""
    from hooks import _redis
    from scripts.routing import reservation

    counted = Mock(side_effect=lambda redis, now: _records())
    monkeypatch.setattr(aggregate, "_scan", counted)
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(_redis, "get_redis", lambda: object())
    monkeypatch.setattr(reservation.master_account, "load", lambda environ: {"claude": SimpleNamespace(slug="one")})
    monkeypatch.setattr(reservation, "master_agents", lambda store, harness, slug: 0)
    monkeypatch.setattr(reservation, "_store", lambda environ: None)
    return counted


@pytest.mark.feature("controller-quota-read-once")
def test_one_pass_makes_one_hive_quota_read_for_every_account(scans: Mock) -> None:
    read = capacity.hive_accounts(cast(RedisStore, _Store()))
    with pool.running():
        first = read(ENVIRON, 100, refresh=False)
        assert read(ENVIRON, 101, refresh=False) == first
        assert capacity.accounts(ENVIRON, 102, refresh=False) == first
    assert scans.call_count == 1
    assert {row.name for row in first} == {"one", "two"}
    with pool.running():
        read(ENVIRON, 103, refresh=False)
    assert scans.call_count == 2


@pytest.mark.feature("controller-quota-read-once")
def test_reads_outside_a_pass_stay_fresh(scans: Mock) -> None:
    read = capacity.hive_accounts(cast(RedisStore, _Store()))
    read(ENVIRON, 100, refresh=False)
    read(ENVIRON, 101, refresh=False)
    assert scans.call_count == 4


@pytest.mark.feature("controller-hive-refusal")
def test_a_hive_reading_marked_refused_closes_that_account_in_the_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    records = _records()
    records["claude:one"]["quota"] = _quota(refused=True)
    monkeypatch.setattr(aggregate, "current", lambda now: records)
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    rows = {row.name: row for row in capacity.accounts(ENVIRON, 100, refresh=False)}
    assert (rows["one"].state, rows["one"].cap, capacity.free_seats(rows["one"])) == ("CLOSED", 0, 0)
    assert rows["two"].state == "OPEN"
    assert capacity.free_seats(rows["two"]) > 0


@pytest.mark.feature("controller-hive-refusal")
@pytest.mark.parametrize("status", [balancer.REVOKED, "rejected"])
def test_a_hive_publishes_a_provider_refusal_with_its_claude_reading(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    refused = balancer.ProbeResult(
        "one", status, "OK", 60, balancer.QuotaWindow(20, 1000), balancer.QuotaWindow(40, 2000)
    )
    allowed = balancer.ProbeResult(
        "two", "allowed", "OK", 60, balancer.QuotaWindow(20, 1000), balancer.QuotaWindow(40, 2000)
    )
    monkeypatch.setattr(daemon, "claude_signed_in", lambda: False)
    monkeypatch.setattr(daemon, "_claude_snapshots", lambda observed: None)
    monkeypatch.setattr(balancer, "cached_observations", Mock(return_value=[(120, refused), (120, allowed)]))
    monkeypatch.setattr(daemon.codex_router, "accounts", Mock(return_value=[]))
    monkeypatch.setattr(daemon.codex_router, "quotas", Mock(return_value={}))
    environ = {"AH_CC_TOKEN_one": "first", "AH_CC_TOKEN_two": "second"}
    _, quota = daemon._subscriptions(environ, {"claude": {}, "codex": {}}, 122)
    assert quota["claude:one"]["refused"] is True
    assert "refused" not in quota["claude:two"]
