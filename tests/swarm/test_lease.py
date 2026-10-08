import json

import pytest

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmError

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def test_renewal_and_expiry_takeover(store, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda saved: clock[0])
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
    monkeypatch.setattr(lease, "now_ms", lambda saved: 1000)
    first = lease.acquire(store, "sw", "home")
    assert lease.release(store, "sw", lease.Lease("other", 1, 181000)) is False
    assert lease.release(store, "sw", first) is True
    assert lease.current(store, "sw") is None
    second = lease.acquire(store, "sw", "home")
    assert second == lease.Lease("home", 2, 181000)
    assert lease.release(store, "sw", first) is False


def test_legacy_owner_keeps_ownership_until_expiry(store, monkeypatch):
    monkeypatch.setattr(lease, "now_ms", lambda saved: 1000)
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
    monkeypatch.setattr(lease, "now_ms", lambda saved: clock[0])
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
    monkeypatch.setattr(lease, "now_ms", lambda saved: 1000)
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


def test_clock_skew_does_not_expire_the_redis_lease(store, monkeypatch):
    import time

    held = lease.acquire(store, "sw", "home")
    monkeypatch.setattr(time, "time_ns", lambda: (held.expires_at + 1) * 1_000_000)
    assert lease.current(store, "sw") == held
    assert lease.acquire(store, "sw", "other") is None


def test_redis_time_is_converted_to_milliseconds(store, monkeypatch):
    monkeypatch.setattr(store.redis, "time", lambda: (2, 234567))
    assert lease.now_ms(store) == 2234


def test_current_absent_and_legacy_owner_and_wrong_identity(store, monkeypatch):
    monkeypatch.setattr(lease, "now_ms", lambda saved: 1000)
    assert lease.current(store, "sw") is None
    store.redis.set(store.key("sw", "control-owner"), "legacy")
    assert lease.current(store, "sw") is None
    held = lease.acquire(store, "sw", "legacy")
    for wrong in (lease.Lease("other", 1, 181000), lease.Lease("legacy", 2, 181000)):
        with pytest.raises(SwarmError) as error:
            lease.require(store, "sw", wrong)
        assert str(error.value) == "the controller lease is stale"
    assert lease.current(store, "sw") == held


def test_acquisition_retries_a_conflicting_transaction(store, monkeypatch):
    from redis.exceptions import WatchError

    pipeline = store.redis.pipeline
    attempts = []

    def conflicting_pipeline():
        pipe = pipeline()
        execute = pipe.execute

        def commit():
            attempts.append(1)
            if len(attempts) == 1:
                raise WatchError("conflict")
            return execute()

        pipe.execute = commit
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", conflicting_pipeline)
    held = lease.acquire(store, "sw", "home")
    assert held.epoch == 1
    assert len(attempts) == 2
    assert lease.current(store, "sw") == held


def test_release_conflict_does_not_remove_the_lease(store, monkeypatch):
    from redis.exceptions import WatchError

    held = lease.acquire(store, "sw", "home")
    pipeline = store.redis.pipeline

    def conflicting_pipeline():
        pipe = pipeline()
        pipe.execute = lambda: (_ for _ in ()).throw(WatchError("conflict"))
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", conflicting_pipeline)
    assert lease.release(store, "sw", held) is False
    assert lease.current(store, "sw") == held


def test_controller_loop_waits_one_tick_between_runs(store, monkeypatch, capsys):
    from scripts import operator_env
    from scripts.swarm import controller

    calls, sleeps = [], []
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(controller, "connect", lambda: store)
    monkeypatch.setattr(controller, "run_once", lambda saved: calls.append(saved) or {"sw": ["worked"]})

    def stop_after_wait(delay):
        sleeps.append(delay)
        raise KeyboardInterrupt

    monkeypatch.setattr(controller.time, "sleep", stop_after_wait)
    with pytest.raises(KeyboardInterrupt):
        controller.main(["run"])
    assert calls == [store]
    assert sleeps == [60.0]
    assert capsys.readouterr().out == "sw: worked\n"


def test_controller_reports_a_lease_error(store, monkeypatch, capsys):
    from scripts import operator_env
    from scripts.swarm import controller

    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(controller, "connect", lambda: store)

    def stale(saved):
        raise SwarmError("the controller lease is stale")

    monkeypatch.setattr(controller, "run_once", stale)
    assert controller.main(["run", "--once"]) == 1
    assert capsys.readouterr().err == "controller: the controller lease is stale\n"


def test_epoch_scope_resets_after_failure():
    assert lease.EPOCH.get() is None
    with pytest.raises(SwarmError):
        with lease.fencing(17):
            assert lease.EPOCH.get() == 17
            raise SwarmError("failed")
    assert lease.EPOCH.get() is None
