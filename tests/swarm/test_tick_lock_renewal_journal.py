"""Each tick lock renewal, and each step hook call that found another call holding the hook, journals its timing."""

import json
from collections.abc import Iterator
from threading import Lock
from types import SimpleNamespace
from typing import cast

import fakeredis
import pytest

from scripts.swarm import tick_activity, timing
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.unit


class HeldLock:
    """A hook lock another call holds when first asked, then granted."""

    def __init__(self) -> None:
        self.asked: list[bool] = []

    def acquire(self, blocking: bool = True) -> bool:
        self.asked.append(blocking)
        return blocking

    def release(self) -> None:
        return None


def clocked(monkeypatch: pytest.MonkeyPatch, readings: list[float]) -> None:
    ticks: Iterator[float] = iter(readings)
    monkeypatch.setattr(tick_activity, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(tick_activity, "tick_stop", SimpleNamespace(check=lambda: None))
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", raising=False)


def journaled(err: str) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("event") == "swarm_tick_lock_renewal"]


def renewal(renewed: list[int], clock: list[int], lock: Lock | None = None) -> tick_activity.Renewal:
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    held = Lock() if lock is None else lock
    return tick_activity.Renewal(store, "sw", lambda: renewed.append(clock[0]), lambda: clock[0], lock=held)


@pytest.mark.feature("swarm-tick-lock-renewal-journal")
def test_a_renewal_journals_its_wait_hold_and_redis_seconds_under_its_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clocked(monkeypatch, [10.0, 13.0, 14.0, 16.5, 17.0])
    renewed: list[int] = []
    hook = renewal(renewed, [1_000])
    token = timing.PASS.set({"pass_id": "p1", "failed_steps": []})
    try:
        hook()
    finally:
        timing.PASS.reset(token)
    assert renewed == [1_000]
    assert journaled(capsys.readouterr().err) == [
        {
            "event": "swarm_tick_lock_renewal",
            "slug": "sw",
            "pass_id": "p1",
            "blocked": False,
            "renewed": True,
            "wait_s": 3.0,
            "wall_s": 4.0,
            "redis_s": 2.5,
        }
    ]


@pytest.mark.feature("swarm-tick-lock-renewal-journal")
def test_a_call_inside_the_interval_that_found_the_hook_free_journals_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clocked(monkeypatch, [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0])
    renewed: list[int] = []
    clock = [1_000]
    hook = renewal(renewed, clock)
    hook()
    capsys.readouterr()
    clock[0] = 1_001
    hook()
    assert renewed == [1_000]
    assert journaled(capsys.readouterr().err) == []


@pytest.mark.feature("swarm-tick-lock-renewal-journal")
def test_a_call_that_found_the_hook_held_journals_its_wait_even_without_a_renewal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clocked(monkeypatch, [20.0, 20.5, 21.0, 22.0, 22.0, 30.0, 34.0, 34.25])
    renewed: list[int] = []
    clock = [1_000]
    held = HeldLock()
    hook = renewal(renewed, clock, cast(Lock, held))
    hook()
    clock[0] = 1_001
    hook()
    assert held.asked == [False, True, False, True]
    assert renewed == [1_000]
    assert journaled(capsys.readouterr().err) == [
        {
            "event": "swarm_tick_lock_renewal",
            "slug": "sw",
            "pass_id": None,
            "blocked": True,
            "renewed": True,
            "wait_s": 0.5,
            "wall_s": 1.5,
            "redis_s": 1.0,
        },
        {
            "event": "swarm_tick_lock_renewal",
            "slug": "sw",
            "pass_id": None,
            "blocked": True,
            "renewed": False,
            "wait_s": 4.0,
            "wall_s": 0.25,
            "redis_s": None,
        },
    ]


@pytest.mark.feature("swarm-tick-lock-renewal-journal")
def test_a_revoked_hook_that_found_itself_free_journals_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clocked(monkeypatch, [10.0, 11.0, 12.0])
    renewed: list[int] = []
    hook = renewal(renewed, [1_000])
    hook.revoke()
    hook.now()
    assert renewed == []
    assert journaled(capsys.readouterr().err) == []
