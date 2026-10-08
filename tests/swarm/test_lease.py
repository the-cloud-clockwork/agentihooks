import json

import fakeredis
import pytest

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmError

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def test_renewal_and_expiry_takeover(store, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda: clock[0])
    first = lease.acquire(store, "sw", "home")
    assert first == lease.Lease("home", 1, 181000)
    assert store.redis.pttl(store.key("sw", "control-owner")) > 179000
    assert lease.acquire(store, "sw", "other") is None
    clock[0] = 2000
    renewed = lease.acquire(store, "sw", "home")
    assert renewed == lease.Lease("home", 1, 182000)
    clock[0] = 182000
    second = lease.acquire(store, "sw", "other")
    assert second == lease.Lease("other", 2, 362000)
    with pytest.raises(SwarmError) as error:
        lease.require(store, "sw", first)
    assert str(error.value) == "the controller lease is stale"
    lease.require(store, "sw", second)


def test_release_preserves_epoch_and_refuses_old_owner(store, monkeypatch):
    monkeypatch.setattr(lease, "now_ms", lambda: 1000)
    first = lease.acquire(store, "sw", "home")
    assert lease.release(store, "sw", lease.Lease("other", 1, 181000)) is False
    assert lease.release(store, "sw", first) is True
    assert lease.current(store, "sw") is None
    second = lease.acquire(store, "sw", "home")
    assert second == lease.Lease("home", 2, 181000)
    assert lease.release(store, "sw", first) is False


def test_legacy_owner_keeps_ownership_until_expiry(store, monkeypatch):
    monkeypatch.setattr(lease, "now_ms", lambda: 1000)
    store.redis.set(store.key("sw", "control-owner"), "legacy")
    assert lease.acquire(store, "sw", "other") is None
    assert lease.current(store, "sw") == lease.Lease("legacy", 1, 181000)
    assert store.redis.pttl(store.key("sw", "control-owner")) > 179000
    assert json.loads(store.redis.get(store.key("sw", "control-owner"))) == {
        "owner": "legacy",
        "epoch": 1,
        "expires_at": 181000,
    }


def test_expired_lease_cannot_write_or_release(store, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda: clock[0])
    first = lease.acquire(store, "sw", "home")
    clock[0] = first.expires_at
    assert lease.current(store, "sw") is None
    assert lease.release(store, "sw", first) is False
    with pytest.raises(SwarmError) as error:
        lease.require(store, "sw", first)
    assert str(error.value) == "the controller lease is stale"


def test_controller_status_and_release(store, monkeypatch, capsys):
    from scripts.swarm import cli, commands
    from scripts.swarm.store import SwarmConfig

    store.create(SwarmConfig("sw", ".", 0, 0))
    monkeypatch.setattr(lease, "now_ms", lambda: 1000)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    held = lease.acquire(store, "sw", commands.hive_id())
    assert cli.main(["sw", "controller"]) == 0
    assert capsys.readouterr().out == '{"owner": "home", "epoch": 1, "expires_at": 181000}\n'
    assert cli.main(["sw", "controller", "release"]) == 0
    assert capsys.readouterr().out == '{"released": true}\n'
    assert lease.current(store, "sw") is None
    assert held.epoch == 1


def test_controller_entrypoint_runs_once_and_reports_actions(store, monkeypatch, capsys):
    from scripts import install, operator_env
    from scripts.swarm import controller

    calls = []
    monkeypatch.setattr(controller, "connect", lambda: store)
    monkeypatch.setattr(controller, "run_once", lambda saved: calls.append(saved) or {"sw": ["worked"]})
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(controller.time, "sleep", lambda delay: pytest.fail("once must not sleep"))
    monkeypatch.setattr(install.sys, "argv", ["agentihooks", "controller", "run", "--once"])
    with pytest.raises(SystemExit) as result:
        install.main()
    assert result.value.code == 0
    assert calls == [store]
    assert capsys.readouterr().out == "sw: worked\n"
