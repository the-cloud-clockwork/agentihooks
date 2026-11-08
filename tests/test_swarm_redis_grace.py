"""Tests for the Redis grace window that keeps the controller alive through a short Redis restart."""

import time
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from scripts.swarm import controller, controller_probes, redis_grace
from scripts.swarm.store import RedisStore, SwarmError, Unreachable

pytestmark = pytest.mark.unit


class StopLoop(Exception):
    pass


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


STORE = cast(RedisStore, None)
PROOF = ModuleType("proof_swarm")


def refused() -> None:
    raise Unreachable("Redis is unreachable (connection refused)")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv(redis_grace.GRACE_ENV, raising=False)
    monkeypatch.setattr("scripts.swarm.controller.connect", refused)
    return tmp_path


def test_grace_defaults_to_two_minutes_and_reads_its_setting():
    assert redis_grace.grace_seconds({}) == 120
    assert redis_grace.grace_seconds({redis_grace.GRACE_ENV: "45"}) == 45


def test_the_marker_lives_under_the_controller_home():
    assert redis_grace.marker({"AGENTIHOOKS_HOME": "/run/x"}) == Path("/run/x/controller/redis-outage")
    assert redis_grace.marker({}) == Path.home() / ".agentihooks" / "controller" / "redis-outage"


def test_the_outage_window_comes_from_the_grace_setting(monkeypatch):
    monkeypatch.setenv(redis_grace.GRACE_ENV, "45")

    assert redis_grace.Outage.from_environ().grace_s == 45


def test_down_for_records_the_first_failure_and_counts_from_it(tmp_path):
    path = tmp_path / "home" / "controller" / "redis-outage"

    assert redis_grace.down_for(path, 100.0) == 0
    assert redis_grace.down_for(path, 130.0) == 30
    redis_grace.cleared(path)
    assert not path.exists()
    redis_grace.cleared(path)


def test_down_for_restarts_the_count_from_an_unreadable_marker(tmp_path):
    path = tmp_path / "redis-outage"
    path.write_text("garbage")

    assert redis_grace.down_for(path, 50.0) == 0
    assert path.read_text() == "50.0"


def test_live_passes_while_redis_refuses_inside_the_grace_window(home, capsys):
    assert controller.main(["live"]) == 0
    assert controller.main(["live"]) == 0
    assert "inside the 120s Redis grace window" in capsys.readouterr().out
    assert (home / "controller" / "redis-outage").exists()


def test_live_fails_once_redis_has_refused_past_the_grace_window(home, capsys):
    marker = home / "controller" / "redis-outage"
    marker.parent.mkdir(parents=True)
    marker.write_text(str(time.time() - 121))

    assert controller.main(["live"]) == 1
    assert capsys.readouterr().out == "controller: Redis is unreachable (connection refused)\n"


def test_live_fails_at_once_with_a_zero_grace_window(home, monkeypatch):
    monkeypatch.setenv(redis_grace.GRACE_ENV, "0")

    assert controller.main(["live"]) == 1


def test_live_rides_out_a_redis_error_raised_by_the_probe_itself(home, monkeypatch):
    import redis

    def dropped(environ, store):
        raise redis.ConnectionError("Connection refused")

    monkeypatch.setattr("scripts.swarm.controller.connect", lambda: "store")
    monkeypatch.setattr(controller_probes, "live", dropped)

    assert controller.main(["live"]) == 0


def test_a_live_probe_that_reaches_redis_clears_the_outage(home, monkeypatch):
    marker = home / "controller" / "redis-outage"
    marker.parent.mkdir(parents=True)
    marker.write_text(str(time.time() - 500))
    monkeypatch.setattr("scripts.swarm.controller.connect", lambda: "store")
    monkeypatch.setattr(controller_probes, "live", lambda environ, store: 0)

    assert controller.main(["live"]) == 0
    assert not marker.exists()


def test_ready_still_fails_while_redis_refuses(home, capsys):
    assert controller.main(["ready"]) == 1
    assert capsys.readouterr().out == "controller: Redis is unreachable (connection refused)\n"
    assert not (home / "controller" / "redis-outage").exists()


def test_the_outage_tolerates_errors_inside_the_window_and_resets_on_success():
    clock = Clock()
    outage = redis_grace.Outage(30, clock)

    assert outage.tolerated()
    clock.now = 29
    assert outage.tolerated()
    clock.now = 30
    assert not outage.tolerated()
    outage.ended()
    assert outage.tolerated()


def passes(monkeypatch, errors: list[BaseException]) -> list[int]:
    calls = []

    def run(store, service, proof_swarm):
        calls.append(1)
        raise errors[len(calls) - 1]

    monkeypatch.setattr(controller, "_pass", run)
    monkeypatch.setattr(controller, "scheduled_wait", lambda seconds: None)
    return calls


def test_the_pass_loop_keeps_running_through_redis_errors_inside_the_window(monkeypatch, capsys):
    import redis

    calls = passes(monkeypatch, [redis.ConnectionError("refused"), redis.ConnectionError("refused"), StopLoop()])

    with pytest.raises(StopLoop):
        controller._passes(STORE, None, PROOF, False, redis_grace.Outage(30, Clock()))
    assert len(calls) == 3
    assert "inside the Redis grace window" in capsys.readouterr().err


def test_the_pass_loop_ends_once_redis_errors_outlast_the_window(monkeypatch):
    import redis

    clock = Clock()
    calls = passes(monkeypatch, [redis.ConnectionError("refused"), redis.ConnectionError("refused")])
    monkeypatch.setattr(controller, "scheduled_wait", lambda seconds: setattr(clock, "now", clock.now + 31))

    with pytest.raises(Unreachable, match="past the 30s grace window"):
        controller._passes(STORE, None, PROOF, False, redis_grace.Outage(30, clock))
    assert len(calls) == 2


def test_a_single_pass_reports_a_tolerated_redis_error_as_failed(monkeypatch):
    import redis

    passes(monkeypatch, [redis.ConnectionError("refused")])

    assert controller._passes(STORE, None, PROOF, True, redis_grace.Outage(30, Clock())) == 1


def test_a_pass_that_reaches_redis_clears_a_probe_outage(home, monkeypatch):
    marker = home / "controller" / "redis-outage"
    marker.parent.mkdir(parents=True)
    marker.write_text(str(time.time() - 500))
    monkeypatch.setattr(controller, "_pass", lambda store, service, proof_swarm: None)

    assert controller._passes(STORE, None, PROOF, True, redis_grace.Outage(30, Clock())) == 0
    assert not marker.exists()


def test_live_fails_at_once_on_a_swarm_error_other_than_redis(home, monkeypatch, capsys):
    def misconfigured():
        raise SwarmError("swarm fixture is not configured")

    monkeypatch.setattr("scripts.swarm.controller.connect", misconfigured)

    assert controller.main(["live"]) == 1
    assert capsys.readouterr().out == "controller: swarm fixture is not configured\n"
    assert not (home / "controller" / "redis-outage").exists()


def test_startup_retries_a_refused_connect_inside_the_window(monkeypatch, capsys):
    attempts = []

    def connect():
        attempts.append(1)
        if len(attempts) < 3:
            raise Unreachable("Redis is unreachable (connection refused)")
        return "store"

    monkeypatch.setattr(controller, "connect", connect)
    monkeypatch.setattr(controller, "scheduled_wait", lambda seconds: None)
    outage = redis_grace.Outage(30, Clock())

    assert controller._reached(outage) == "store"
    assert len(attempts) == 3
    assert outage.since is None
    assert capsys.readouterr() == (
        "",
        "controller: Redis is unreachable (connection refused); retrying after one tick inside the Redis grace window\n"
        * 2,
    )


def test_startup_gives_up_once_the_refusal_outlasts_the_window(monkeypatch):
    clock = Clock()

    def connect():
        raise Unreachable("Redis is unreachable (connection refused)")

    monkeypatch.setattr(controller, "connect", connect)
    monkeypatch.setattr(controller, "scheduled_wait", lambda seconds: setattr(clock, "now", clock.now + 31))

    with pytest.raises(Unreachable):
        controller._reached(redis_grace.Outage(30, clock))
