import sys
from collections.abc import Callable

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from scripts.ci_lab import scheduler
from tests.ci_lab.test_scheduler import FLUSHED, HEAD_A, CountingRelay, Run, SilentRelay, Stdout, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab


@pytest.mark.feature("lab-scheduler-serve-survives-redis-errors")
def test_serve_skips_a_pass_whose_tick_raises_a_redis_error_and_runs_the_next_on_schedule(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=60_000)
    run.push(HEAD_A)
    out = Stdout()
    monkeypatch.setattr(sys, "stdout", out)
    relaying = CountingRelay(out)
    tick = run.engine.tick
    failures = [RedisTimeoutError("Timeout reading from socket")]

    def flaky(now_ms: int) -> list[str]:
        if failures:
            raise failures.pop()
        return tick(now_ms)

    def sleep(seconds: float) -> None:
        out.events.append(f"slept {seconds}")

    monkeypatch.setattr(run.engine, "tick", flaky)
    assert scheduler.serve(run.engine, relaying, 0.25, 2, lambda: 2.5, sleep) == 0
    assert relaying.ticks == 1
    assert out.events == [
        "pass 1 skipped on TimeoutError: Timeout reading from socket",
        "\n",
        FLUSHED,
        "slept 0.25",
        f"received {HEAD_A} on feature",
        "\n",
        FLUSHED,
        "tick 1",
        "relayed 1",
        "\n",
        FLUSHED,
        "slept 0.25",
    ]


@pytest.mark.feature("lab-scheduler-serve-survives-redis-errors")
def test_serve_skips_a_pass_whose_relay_tick_raises_a_redis_error_and_runs_the_next_on_schedule(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=60_000)
    run.push(HEAD_A)
    out = Stdout()
    monkeypatch.setattr(sys, "stdout", out)
    relaying = CountingRelay(out)
    relay_tick = relaying.tick
    failures = [RedisConnectionError("Connection reset by peer")]

    def flaky() -> list[str]:
        if failures:
            raise failures.pop()
        return relay_tick()

    def sleep(seconds: float) -> None:
        out.events.append(f"slept {seconds}")

    monkeypatch.setattr(relaying, "tick", flaky)
    assert scheduler.serve(run.engine, relaying, 0.25, 2, lambda: 2.5, sleep) == 0
    assert relaying.ticks == 1
    assert out.events == [
        f"received {HEAD_A} on feature",
        "\n",
        FLUSHED,
        "pass 1 skipped on ConnectionError: Connection reset by peer",
        "\n",
        FLUSHED,
        "slept 0.25",
        "tick 1",
        "relayed 1",
        "\n",
        FLUSHED,
        "slept 0.25",
    ]


@pytest.mark.feature("lab-scheduler-serve-survives-redis-errors")
def test_serve_ends_on_an_error_that_is_not_a_redis_error(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({})
    slept: list[float] = []

    def broken(now_ms: int) -> list[str]:
        raise ValueError(f"no pass at {now_ms}")

    monkeypatch.setattr(run.engine, "tick", broken)
    with pytest.raises(ValueError, match="^no pass at 1000$"):
        scheduler.serve(run.engine, SilentRelay(), 1.0, 2, lambda: 1.0, slept.append)
    assert slept == []
