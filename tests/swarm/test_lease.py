import json

import pytest

from scripts.events import state
from scripts.events.bus import StateConflict
from scripts.swarm import fencing, lease
from scripts.swarm.store import RedisStore, SwarmError

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def _seed(store, **fields):
    fencing.write(store, "sw", lambda doc: doc.update(fields))


class _Racing:
    def __init__(self, inner, competitors):
        self.inner, self.competitors, self.updates = inner, competitors, 0

    def get(self, key):
        return self.inner.get(key)

    def create(self, key, value):
        return self.inner.create(key, value)

    def update(self, key, value, revision):
        self.updates += 1
        if self.competitors:
            self.competitors.pop(0)(key)
        return self.inner.update(key, value, revision)


def _racing(store, monkeypatch, *competitors):
    racing = _Racing(state.for_store(store, fencing.BUCKET), list(competitors))
    monkeypatch.setattr(fencing.state, "for_store", lambda *args, **kwargs: racing)
    return racing


def _race(store, monkeypatch, **fields):
    states = state.for_store(store, fencing.BUCKET)

    def compete(key):
        entry = states.get(key)
        states.update(key, json.dumps({**json.loads(entry.value), **fields}).encode(), entry.revision)

    return _racing(store, monkeypatch, compete)


def _conflict(key):
    raise StateConflict("raced")


def test_renewal_and_expiry_takeover(store, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda saved: clock[0])
    first = lease.acquire(store, "sw", "home")
    assert first == lease.Lease("home", 1, 181000)
    assert fencing.read(store, "sw")["lease"] == {"owner": "home", "epoch": 1, "expires_at": 181000}
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


def test_renewal_and_release_read_the_clock_of_their_own_store(store, monkeypatch):
    monkeypatch.setattr(lease, "now_ms", lambda given: 1000 if given is store else 10**12)
    held = lease.acquire(store, "sw", "home")

    assert held == lease.Lease("home", 1, 1000 + lease.ttl_ms())
    assert lease.renew(store, "sw", held) == held
    assert lease.release(store, "sw", held) is True
    assert fencing.read(store, "sw")["lease"] is None


def test_a_hosted_fenced_write_checks_the_hosted_lease(store, monkeypatch):
    from dataclasses import asdict

    held = lease.Lease("home", 1, 10**13)
    monkeypatch.setattr(lease, "_address", lambda slug: "https://ledger.example")
    monkeypatch.setattr(lease, "_remote", lambda slug, action, fields=None: asdict(held))

    assert lease.fenced(store, "sw", held, lambda doc: doc.update(epoch=7) or "done") == "done"
    assert fencing.read(store, "sw")["epoch"] == 7


def test_legacy_owner_keeps_ownership_until_expiry(store, monkeypatch):
    monkeypatch.setattr(lease, "now_ms", lambda saved: 1000)
    store.redis.set(store.key("sw", "control-owner"), "legacy")
    assert lease.acquire(store, "sw", "other") is None
    assert lease.current(store, "sw") == lease.Lease("legacy", 1, 181000)
    assert fencing.read(store, "sw")["lease"] == {
        "owner": "legacy",
        "epoch": 1,
        "expires_at": 181000,
    }


@pytest.mark.parametrize("legacy", [False, True])
def test_seeded_hive_adopts_its_hostname_lease(store, monkeypatch, legacy):
    import socket

    from scripts.swarm import commands, controller

    monkeypatch.delenv("SWARM_HIVE_ID", raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "this-machine")
    monkeypatch.setattr(commands.registry, "seeded_id", lambda: None)
    monkeypatch.setattr(lease, "now_ms", lambda saved: 1000)
    held = lease.acquire(store, "sw", commands.hive_id())
    assert held == lease.Lease("this-machine", 1, 181000)
    if legacy:
        _seed(store, lease=None, legacy_owner="this-machine")
    monkeypatch.setattr(commands.registry, "seeded_id", lambda: "seeded-hive")
    migrated = lease.acquire(store, "sw", commands.hive_id())
    assert migrated == lease.Lease("seeded-hive", 1 if not legacy else 2, 181000)
    assert lease.current(store, "sw") == migrated
    lease.require_epoch(store, "sw", migrated.epoch)
    assert controller.take_tick_lock(store, "sw", migrated, 1000) is not None
    with pytest.raises(lease.SwarmError, match="the controller lease is stale"):
        lease.require(store, "sw", held)
    assert lease.acquire(store, "sw", "other-hive") is None


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("holder, requested", [("other-machine", "seeded-hive"), ("this-machine", "other-hive")])
def test_seeded_hive_never_adopts_another_machine_lease(store, monkeypatch, legacy, holder, requested):
    import socket

    from scripts.swarm import commands

    monkeypatch.setattr(socket, "gethostname", lambda: "this-machine")
    monkeypatch.setattr(commands.registry, "seeded_id", lambda: "seeded-hive")
    monkeypatch.delenv("SWARM_HIVE_ID", raising=False)
    monkeypatch.setattr(lease, "now_ms", lambda saved: 1000)
    held = lease.acquire(store, "sw", holder)
    if legacy:
        _seed(store, lease=None, legacy_owner=holder)
    assert lease.acquire(store, "sw", requested) is None
    assert lease.current(store, "sw") == lease.Lease(holder, 2 if legacy else held.epoch, 181000)


def test_hostname_migration_rechecks_a_competing_owner(store, monkeypatch):
    import socket

    from scripts.swarm import commands

    monkeypatch.setattr(socket, "gethostname", lambda: "this-machine")
    monkeypatch.setattr(commands.registry, "seeded_id", lambda: "seeded-hive")
    monkeypatch.delenv("SWARM_HIVE_ID", raising=False)
    monkeypatch.setattr(lease, "now_ms", lambda saved: 1000)
    lease.acquire(store, "sw", "this-machine")
    _race(store, monkeypatch, lease={"owner": "other-machine", "epoch": 2, "expires_at": 181000}, epoch=2)
    assert lease.acquire(store, "sw", commands.hive_id()) is None
    assert lease.current(store, "sw") == lease.Lease("other-machine", 2, 181000)


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
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "scheduled_wait", lambda delay: pytest.fail("once must not sleep"))
    monkeypatch.setattr(install.sys, "argv", ["agentihooks", "controller", "run", "--once"])
    with pytest.raises(SystemExit) as result:
        install.main()
    assert result.value.code == 0
    assert calls == [store]
    assert capsys.readouterr().out == "sw: worked\n"


def test_the_lease_expires_on_the_host_wall_clock(store, monkeypatch):
    import time

    held = lease.acquire(store, "sw", "home")
    monkeypatch.setattr(time, "time", lambda: (held.expires_at - 1) / 1000)
    assert lease.current(store, "sw") == held
    monkeypatch.setattr(time, "time", lambda: held.expires_at / 1000)
    assert lease.current(store, "sw") is None
    assert lease.acquire(store, "sw", "other") == lease.Lease("other", 2, held.expires_at + lease.ttl_ms())


@pytest.mark.parametrize("seconds", [2.2341, 2.2339, 2.234])
def test_the_host_clock_is_converted_to_milliseconds(store, monkeypatch, seconds):
    import time

    monkeypatch.setattr(time, "time", lambda: seconds)
    assert lease.now_ms(store) == 2234


def test_current_absent_and_legacy_owner_and_wrong_identity(store, monkeypatch):
    monkeypatch.setattr(lease, "now_ms", lambda saved: 1000)
    assert lease.current(store, "sw") is None
    _seed(store, legacy_owner="legacy")
    assert lease.current(store, "sw") is None
    held = lease.acquire(store, "sw", "legacy")
    for wrong in (lease.Lease("other", 1, 181000), lease.Lease("legacy", 2, 181000)):
        with pytest.raises(SwarmError) as error:
            lease.require(store, "sw", wrong)
        assert str(error.value) == "the controller lease is stale"
    assert lease.current(store, "sw") == held


def test_acquisition_retries_a_conflicting_transaction(store, monkeypatch):
    racing = _racing(store, monkeypatch, _conflict)
    held = lease.acquire(store, "sw", "home")
    assert held.epoch == 1
    assert racing.updates == 2
    assert lease.current(store, "sw") == held


def test_release_conflict_does_not_remove_the_lease(store, monkeypatch):
    held = lease.acquire(store, "sw", "home")
    _racing(store, monkeypatch, *[_conflict] * fencing.ATTEMPTS)
    assert lease.release(store, "sw", held) is False
    assert lease.current(store, "sw") == held


def test_controller_loop_waits_one_tick_between_runs(store, monkeypatch, capsys):
    from scripts import operator_env
    from scripts.swarm import controller

    calls, sleeps = [], []
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "connect", lambda: store)
    monkeypatch.setattr(controller, "run_once", lambda saved: calls.append(saved) or {"sw": ["worked"]})

    def stop_after_wait(delay):
        sleeps.append(delay)
        raise KeyboardInterrupt

    monkeypatch.setattr(controller, "scheduled_wait", stop_after_wait)
    with pytest.raises(KeyboardInterrupt):
        controller.main(["run"])
    assert calls == [store]
    assert sleeps == [60.0]
    assert capsys.readouterr().out == "sw: worked\n"


def test_controller_reports_a_lease_error(store, monkeypatch, capsys):
    from scripts import operator_env
    from scripts.swarm import controller

    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
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


def test_epoch_counter_continues_from_the_imported_swarm_key(store):
    store.redis.set(store.key("sw", "control-epoch"), 7)
    held = lease.acquire(store, "sw", "home")
    assert held.epoch == 8
    assert fencing.read(store, "sw")["epoch"] == 8
    other = lease.acquire(store, "other", "home")
    assert other.epoch == 1


def test_require_epoch_accepts_current_and_refuses_absent(store):
    held = lease.acquire(store, "sw", "home")
    assert lease.require_epoch(store, "sw", held.epoch) is None
    with pytest.raises(SwarmError) as error:
        lease.require_epoch(store, "absent", held.epoch)
    assert str(error.value) == "the controller lease is stale"


@pytest.mark.parametrize("operand", ["lease", "epoch"])
def test_takeover_rereads_a_competing_lease_or_epoch(store, monkeypatch, operand):
    fencing.read(store, "sw")
    competing = (
        {"epoch": 7}
        if operand == "epoch"
        else {"lease": {"owner": "other", "epoch": 7, "expires_at": lease.now_ms(store) + 180000}, "epoch": 7}
    )
    _race(store, monkeypatch, **competing)
    held = lease.acquire(store, "sw", "home")
    if operand == "epoch":
        assert held.epoch == 8
    else:
        assert held is None
        assert lease.current(store, "sw").owner == "other"


def test_controller_without_a_lease_prints_empty_status(store, monkeypatch, capsys):
    from scripts.swarm import cli
    from scripts.swarm.store import SwarmConfig

    store.create(SwarmConfig("sw", ".", 0, 0))
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
    assert cli.main(["sw", "controller"]) == 0
    assert capsys.readouterr().out == '{"owner": "", "epoch": 0, "expires_at": 0}\n'
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(["sw", "controller", "invalid"])
    assert error.value.code == 2


def test_controller_rejects_unknown_actions_with_its_own_usage(capsys, monkeypatch):
    from scripts.swarm import controller

    monkeypatch.setattr(controller, "connect", lambda: pytest.fail("unknown action must refuse before connecting"))
    with pytest.raises(SystemExit) as error:
        controller.main(["invalid"])
    assert error.value.code == 2
    text = capsys.readouterr().err
    assert text in (
        "usage: agentihooks controller [-h] [--once] {run,live,ready}\n"
        "agentihooks controller: error: argument action: invalid choice: 'invalid' "
        "(choose from 'run', 'live', 'ready')\n",
        "usage: agentihooks controller [-h] [--once] {run,live,ready}\n"
        "agentihooks controller: error: argument action: invalid choice: 'invalid' (choose from run, live, ready)\n",
    )


def test_controller_loads_the_environment_and_flushes_actions(store, monkeypatch):
    from scripts import operator_env
    from scripts.swarm import controller

    calls = []
    monkeypatch.setattr(operator_env, "fill", lambda env: calls.append(env))
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "connect", lambda: store)
    monkeypatch.setattr(controller, "run_once", lambda saved: {"sw": ["worked"]})
    printed = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append((args, kwargs)))
    assert controller.main(["run", "--once"]) == 0
    assert calls == [controller.os.environ]
    assert printed == [(("sw: worked",), {"flush": True})]


def test_the_controller_never_ticks_a_host_ticked_swarm(store, monkeypatch):
    from scripts.swarm import cli, controller
    from scripts.swarm.store import SwarmConfig

    for slug in ("hosted", "workstation", "doctor"):
        store.create(SwarmConfig(slug, "agentihooks", 2, 0))
    ticks = []
    monkeypatch.setattr(cli, "run_tick", lambda saved, slug, *args, **kwargs: ticks.append(slug) or [slug])
    monkeypatch.setenv("AGENTIHOOKS_CONTROL_HOST_SWARMS", "workstation,doctor")

    assert controller.run_once(store) == {"hosted": ["hosted"]}
    assert ticks == ["hosted"]


def test_the_controller_ticks_every_swarm_while_no_host_ticked_list_is_set(store, monkeypatch):
    from scripts.swarm import cli, controller
    from scripts.swarm.store import SwarmConfig

    store.create(SwarmConfig("XXXX", "agentihooks", 2, 0))
    monkeypatch.setattr(cli, "run_tick", lambda saved, slug, *args, **kwargs: [slug])
    monkeypatch.delenv("AGENTIHOOKS_CONTROL_HOST_SWARMS", raising=False)

    assert controller.run_once(store) == {"XXXX": ["XXXX"]}
