import pytest

from scripts.swarm import cli, commands
from scripts.swarm.status import status_report
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr


def test_status_separates_current_tick_activity_from_old_completion(env, monkeypatch):  # noqa: F811
    store, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    assert commands.view(store, "sw")["last_tick_activity"] == 0
    assert status_report(store, "sw", ledger.state("sw"))["last_tick_activity"] == 0
    store.update("sw", state="running")
    store.redis.set(store.key("sw", "last-tick"), 100)
    commands.publish(store, "sw", {"last_tick": 100, "last_tick_activity": 0}, {})
    monkeypatch.setattr(cli, "now_ms", lambda: 1_000_000)
    readings = []
    launch = runtime.spawn

    def observe_launch(*args, **kwargs):
        readings.extend([commands.view(store, "sw"), status_report(store, "sw", ledger.state("sw"))])
        return launch(*args, **kwargs)

    monkeypatch.setattr(runtime, "spawn", observe_launch)
    cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert readings
    assert {(row["last_tick_activity"], row["last_tick"]) for row in readings} == {(1_000_000, 100)}
    assert commands.view(store, "sw")["last_tick"] == 1_000_000


def test_a_failed_tick_records_activity_without_claiming_completion(env, monkeypatch):  # noqa: F811
    store, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    store.redis.set(store.key("sw", "last-tick"), 100)
    clock = [200]
    monkeypatch.setattr(cli, "now_ms", lambda: clock[0])

    def fail(slug):
        clock[0] = 300
        cli.timing.keep()
        raise RuntimeError("ledger unavailable")

    ledger.state = fail
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    row = commands.view(store, "sw")
    assert row["last_tick_activity"] == 300
    assert int(store.redis.get(store.key("sw", "last-tick"))) == 100


def test_a_lock_skipped_tick_does_not_claim_new_activity(env, monkeypatch):  # noqa: F811
    store, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    store.redis.set(store.key("sw", "tick-lock"), "another owner")
    store.redis.set(store.key("sw", "last-tick-activity"), 123)
    monkeypatch.setattr(cli, "now_ms", lambda: 999)
    assert cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({})) == ["another tick is running"]
    assert commands.view(store, "sw")["last_tick_activity"] == 123


def test_lost_fencing_cannot_publish_fresh_activity(env):  # noqa: F811
    from scripts.swarm import tick_activity

    store, _, _ = env
    store.redis.set(store.key("sw", "last-tick-activity"), 123)

    def lost():
        raise RuntimeError("stale owner")

    with pytest.raises(RuntimeError, match="stale owner"):
        tick_activity.keep(store, "sw", lost, lambda: 999)
    assert tick_activity.read(store, "sw") == 123
