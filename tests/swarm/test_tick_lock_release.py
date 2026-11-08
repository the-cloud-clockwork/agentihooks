"""A pass releases its tick lock even when closing its pass stack raises, so the next pass takes the lock."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest

from scripts.swarm import cli, controller
from scripts.swarm.ledger_client import LedgerUnreachable
from tests.swarm.test_cli import env, run
from tests.swarm.test_delivery import FakeHerdr

__all__ = ["env"]

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-tick-lock")
def test_a_pass_whose_stack_close_raises_still_releases_its_tick_lock(
    env: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    running = cli.pool.running

    @contextmanager
    def failing_close(**_: Any) -> Generator[None, None, None]:
        yield
        raise LedgerUnreachable("the heartbeat timed out")

    monkeypatch.setattr(cli.pool, "running", failing_close)
    with pytest.raises(LedgerUnreachable, match="the heartbeat timed out"):
        cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert store.redis.get(store.key("sw", "tick-lock")) is None
    monkeypatch.setattr(cli.pool, "running", running)
    take = controller.take_tick_lock
    taken: list[str | None] = []

    def taking(*args: Any) -> str | None:
        taken.append(take(*args))
        return taken[-1]

    monkeypatch.setattr(controller, "take_tick_lock", taking)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert len(taken) == 1
    assert taken[0] is not None
    assert store.redis.get(store.key("sw", "tick-lock")) is None


@pytest.mark.feature("swarm-tick-lock")
def test_a_pass_whose_lock_release_fails_leaves_a_lock_the_next_pass_of_this_process_takes(
    env: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import ConnectionError as RedisDown

    from scripts.swarm import tick_stop

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    release = controller.release_tick_lock
    failed: list[str] = []

    def down(*_: Any) -> None:
        raise RedisDown("Redis is restarting")

    def failing_release(*args: Any) -> None:
        if failed:
            release(*args)
            return
        failed.append(args[2])
        with monkeypatch.context() as patched:
            patched.setattr(controller, "_lock_raw", down)
            release(*args)

    monkeypatch.setattr(controller, "release_tick_lock", failing_release)
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == ["Redis operation failed: ConnectionError"]
    assert store.redis.get(store.key("sw", "tick-lock")) == failed[0]
    assert failed[0] not in tick_stop._HELD
    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert "another tick is running" not in actions
    assert store.redis.get(store.key("sw", "tick-lock")) is None
