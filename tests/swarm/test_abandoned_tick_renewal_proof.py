import json
from functools import partial
from threading import Event
from typing import Any

import fakeredis
import pytest

from scripts.swarm import controller, lease, metrics, timing
from scripts.swarm.store import RedisStore
from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Result
from tests.tick.test_engine import FakeStep, View


@pytest.mark.feature("swarm-tick-lock")
def test_abandoned_recorder_renews_after_release_without_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    held = lease.acquire(store, "sw", "host")
    assert held is not None
    token = controller.take_tick_lock(store, "sw", held, 600_000, "proof")
    assert token is not None
    waiting, resume, ended = Event(), Event(), Event()
    errors: list[BaseException] = []
    events: list[str] = []

    class Box:
        def append(self, table: Any, rows: Any) -> None:
            waiting.set()
            assert resume.wait(5)

        def flush(self, now_ms: int) -> None:
            events.append("flush")

        def close(self) -> None:
            return None

    def settings(environ: Any) -> object:
        return object()

    def outbox(path: Any, sink: Any) -> Box:
        return Box()

    def extra(*args: Any, **kwargs: Any) -> None:
        return None

    def record(ctx: Context) -> Result:
        try:
            timing.call(metrics.record_pass, "sw", 1, 0)
        except BaseException as exc:
            events.append("stale")
            errors.append(exc)
        finally:
            ended.set()
        return Result()

    monkeypatch.setattr(metrics.metrics_outbox, "settings", settings)
    monkeypatch.setattr(metrics.metrics_outbox, "Outbox", outbox)
    monkeypatch.setattr(metrics, "_pass", extra)
    keeping = timing.BEFORE_STEP.set(partial(controller.keep_tick, store, "sw", held, token, 600_000, "proof"))
    try:
        with timing.tick("sw", store):
            result = engine._run_once(
                FakeStep("metrics record", record, budget_s=0.1), Context(View(), Deadline.after(5))
            )
            assert waiting.is_set()
            assert result.abandoned
            ttl = store.redis.pttl(store.key("sw", "tick-lock"))
            assert ttl > 590_000
            controller.release_tick_lock(store, "sw", token, "proof")
            events.append("released")
            resume.set()
            assert ended.wait(5)
            worker = engine._RUNNING[("sw", "metrics record")]
            worker.join(5)
            assert not worker.is_alive()
        assert events == ["released", "stale"]
        assert len(errors) == 1
        assert "the tick lock is stale" in str(errors[0])
        print(json.dumps({"events": events, "ttl_ms_before_release": ttl, "error": str(errors[0])}))
    finally:
        resume.set()
        timing.BEFORE_STEP.reset(keeping)
        controller.release_tick_lock(store, "sw", token, "proof cleanup")
