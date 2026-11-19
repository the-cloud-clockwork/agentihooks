from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import cli, commands, controller, lease, tick_activity
from scripts.swarm.status import status_report
from scripts.swarm.store import RedisStore, SwarmError
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


@pytest.mark.feature("swarm-tick-lock-renewal-interval")
def test_a_pass_renews_its_tick_lock_once_while_every_step_checks_for_a_stop(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state="running")
    keep = controller.keep_tick
    callers: list[str] = []
    checks: list[int] = []

    def keeping(*args: Any) -> None:
        callers.append(args[5])
        keep(*args)

    monkeypatch.setattr(controller, "keep_tick", keeping)
    monkeypatch.setattr(tick_activity, "tick_stop", SimpleNamespace(check=lambda: checks.append(len(callers))))
    monkeypatch.setattr(cli, "now_ms", lambda: 1_000_000)
    cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert callers.count("pass step") == 1
    assert len(checks) > 1


@pytest.mark.feature("swarm-tick-lock-renewal-interval")
def test_the_step_hook_renews_again_once_its_interval_passed_or_the_clock_went_back(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = env[0]
    start, interval = 1_000, tick_activity.RENEW_MS
    clock: list[int] = [start]
    events: list[tuple[str, int]] = []
    monkeypatch.setattr(tick_activity, "tick_stop", SimpleNamespace(check=lambda: events.append(("stop", clock[0]))))
    renewal = tick_activity.Renewal(store, "sw", lambda: events.append(("renew", clock[0])), lambda: clock[0])
    readings = [start, start, start + interval - 1, start + interval, start + interval + 1, start]
    for at in readings:
        clock[0] = at
        renewal()
    assert events == [
        ("stop", start),
        ("renew", start),
        ("stop", start),
        ("stop", start + interval - 1),
        ("stop", start + interval),
        ("renew", start + interval),
        ("stop", start + interval + 1),
        ("stop", start),
        ("renew", start),
    ]
    assert tick_activity.read(store, "sw") == start


@pytest.mark.feature("swarm-tick-lock-renewal-interval")
def test_a_stolen_tick_lock_stops_every_step_from_the_first_renewal_after_the_interval(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
) -> None:
    store = env[0]
    run("sw", "create", "--repo", "/repo")
    held = lease.acquire(store, "sw", commands.hive_id())
    assert held is not None
    token = controller.take_tick_lock(store, "sw", held, cli.TICK_LOCK_MS, "pass")
    assert token is not None
    clock = [1_000]
    renewal = tick_activity.Renewal(
        store,
        "sw",
        lambda: controller.keep_tick(store, "sw", held, token, cli.TICK_LOCK_MS, "pass step"),
        lambda: clock[0],
    )
    renewal()
    store.redis.set(store.key("sw", "tick-lock"), "another owner")
    renewal()
    clock[0] += tick_activity.RENEW_MS
    for _ in range(2):
        with pytest.raises(SwarmError, match="the tick lock is stale"):
            renewal()
    assert tick_activity.read(store, "sw") == 1_000
    assert tick_activity.RENEW_MS * 10 <= cli.TICK_LOCK_MS
