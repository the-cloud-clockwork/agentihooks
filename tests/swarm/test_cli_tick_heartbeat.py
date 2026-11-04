"""The tick command's critical lane heartbeat keeps the tick lock under the pass's own lease and token."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest

from scripts.swarm import cli, controller, lease
from scripts.tick import pool
from tests.swarm.test_cli import env, run
from tests.swarm.test_delivery import FakeHerdr

__all__ = ["env"]


def test_the_tick_heartbeat_keeps_the_tick_lock_with_the_exact_lease_and_token(
    env: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat handed to the pass keeps the tick lock with the held lease, the lock token and the lock time."""
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    real_acquire, real_take_tick_lock, real_running = lease.acquire, controller.take_tick_lock, pool.running
    seen: dict[str, Any] = {}

    def wrap_acquire(*args: Any, **kwargs: Any) -> Any:
        held = real_acquire(*args, **kwargs)
        seen.setdefault("held", held)
        return held

    def wrap_take_tick_lock(*args: Any, **kwargs: Any) -> Any:
        seen["token"] = real_take_tick_lock(*args, **kwargs)
        return seen["token"]

    @contextmanager
    def recording(*args: Any, **kwargs: Any) -> Generator[pool.Pass, None, None]:
        seen["heartbeat"] = kwargs.get("heartbeat")
        with real_running(*args, **kwargs) as found:
            yield found

    keep_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(lease, "acquire", wrap_acquire)
    monkeypatch.setattr(controller, "take_tick_lock", wrap_take_tick_lock)
    monkeypatch.setattr(pool, "running", recording)
    monkeypatch.setattr(controller, "keep_tick", lambda *args: keep_calls.append(args))
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    keep_calls.clear()
    seen["heartbeat"]()
    assert keep_calls == [(store, "sw", seen["held"], seen["token"], cli.TICK_LOCK_MS, "pass heartbeat")]
