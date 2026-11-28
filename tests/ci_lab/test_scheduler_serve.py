import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from scripts.ci_lab import scheduler
from tests.ci_lab.test_scheduler import FLUSHED, HEAD_A, CountingRelay, Run, SilentRelay, Stdout, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab
TIMEOUT = RedisTimeoutError("Timeout reading from socket")
SKIPPED = "skipped on TimeoutError: Timeout reading from socket"


@dataclass
class FakeClock:
    """A serve clock that each sleep moves on by its seconds, failing the test once serve sleeps past its limit."""

    limit: int
    now: float = 0.0
    slept: list[float] = field(default_factory=list[float])

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if len(self.slept) == self.limit:
            pytest.fail(f"serve still running after {self.limit} sleeps")
        self.slept.append(seconds)
        self.now += seconds


def scripted(outcomes: Sequence[RedisError | None]) -> Callable[[int], list[str]]:
    """A scheduler tick that takes the next outcome each pass: an error is raised, None returns one action."""
    pending = list(outcomes)

    def tick(now_ms: int) -> list[str]:
        outcome = pending.pop(0)
        if outcome is not None:
            raise outcome
        return [f"tick at {now_ms}"]

    return tick


def printed(*lines: str) -> list[str]:
    """The stdout events of printing and flushing each line in turn."""
    return [event for line in lines for event in (line, "\n", FLUSHED)]


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


@pytest.mark.feature("lab-scheduler-exits-after-redis-grace")
def test_serve_exits_1_once_redis_errors_outlast_the_grace_window(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({})
    out = Stdout()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(run.engine, "tick", scripted([TIMEOUT] * 3))
    fake = FakeClock(limit=2)
    assert scheduler.serve(run.engine, SilentRelay(), 1.0, 0, fake.clock, fake.sleep, grace_s=2.0) == 1
    assert fake.slept == [1.0, 1.0]
    assert out.events == printed(
        f"pass 1 {SKIPPED}",
        f"pass 2 {SKIPPED}",
        "pass 3 ends the scheduler: Redis failed every pass for the 2s grace window, "
        "last on TimeoutError: Timeout reading from socket",
    )


@pytest.mark.feature("lab-scheduler-exits-after-redis-grace")
def test_serve_rides_out_redis_errors_shorter_than_the_grace_window(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({})
    out = Stdout()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(run.engine, "tick", scripted([TIMEOUT] * 3))
    fake = FakeClock(limit=3)
    assert scheduler.serve(run.engine, SilentRelay(), 1.0, 3, fake.clock, fake.sleep, grace_s=2.5) == 0
    assert fake.slept == [1.0, 1.0, 1.0]
    assert out.events == printed(f"pass 1 {SKIPPED}", f"pass 2 {SKIPPED}", f"pass 3 {SKIPPED}")


@pytest.mark.feature("lab-scheduler-exits-after-redis-grace")
def test_a_good_pass_resets_the_grace_window(lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch) -> None:
    run = lab({})
    out = Stdout()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(run.engine, "tick", scripted([TIMEOUT, TIMEOUT, None, TIMEOUT, TIMEOUT, TIMEOUT]))
    fake = FakeClock(limit=5)
    assert scheduler.serve(run.engine, SilentRelay(), 1.0, 0, fake.clock, fake.sleep, grace_s=2.0) == 1
    assert fake.slept == [1.0] * 5
    assert out.events == printed(
        f"pass 1 {SKIPPED}",
        f"pass 2 {SKIPPED}",
        "tick at 2000",
        f"pass 4 {SKIPPED}",
        f"pass 5 {SKIPPED}",
        "pass 6 ends the scheduler: Redis failed every pass for the 2s grace window, "
        "last on TimeoutError: Timeout reading from socket",
    )
