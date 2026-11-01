import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from scripts.swarm import tick_stop, timing

pytestmark = pytest.mark.unit


def monotonic_clock(readings: list[float]) -> Any:
    """Replace the timing module's clock only, leaving the global time module to other callers."""
    clock = SimpleNamespace(monotonic=iter(readings).__next__, perf_counter=time.perf_counter, time_ns=time.time_ns)
    return patch.object(timing, "time", clock)


@pytest.mark.feature("swarm-tick-cost-report")
@pytest.mark.parametrize("outcome", ["success", "error", "stopped"])
def test_step_history_is_bounded_and_keeps_the_journal(outcome: str, capsys: pytest.CaptureFixture[str]) -> None:
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    failure = {"error": ValueError("private message"), "stopped": tick_stop.Stopped()}.get(outcome)
    with patch.object(store.redis, "xadd", wraps=store.redis.xadd) as append:
        with timing.tick("sw", store):
            try:
                with timing.step("launch"):
                    if failure is not None:
                        raise failure
            except (ValueError, tick_stop.Stopped) as exc:
                assert exc is failure
        assert append.call_count == 1
        assert append.call_args.kwargs == {"maxlen": 50000, "approximate": False}
    rows = store.redis.xrange(store.key("sw", "tick:steps"))
    assert len(rows) == 1
    record = json.loads(rows[0][1]["record"])
    journal = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert record == journal[-1]
    assert record["step"] == "launch"
    assert record["outcome"] == outcome
    assert "private message" not in rows[0][1]["record"]


def test_recording_scope_restores_the_parent_store() -> None:
    import fakeredis

    from scripts.swarm.store import RedisStore

    parent = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    child = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    with timing.tick("outer", parent):
        with timing.tick("inner", child), timing.step("inner"):
            pass
        with timing.step("outer"):
            pass
    with timing.step("outside"):
        pass
    assert parent.redis.xlen(parent.key("outer", "tick:steps")) == 1
    assert child.redis.xlen(child.key("inner", "tick:steps")) == 1
    assert parent.redis.xlen(parent.key("outside", "tick:steps")) == 0


@pytest.mark.feature("swarm-tick-cost-report")
@pytest.mark.parametrize(("ended", "outcome"), [(12.5, "success"), (12.6, "late")])
def test_budgeted_step_record_reads_late_past_its_budget_and_not_failed(ended: float, outcome: str) -> None:
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))

    @timing.instrument_tick
    def run_tick(actual: RedisStore, slug: str) -> list[str]:
        with monotonic_clock([10.0, ended]):
            assert timing.within(2.5, sorted, [2, 1]) == [1, 2]
        return ["finished"]

    assert run_tick(store, "sw") == ["finished"]
    rows = [json.loads(fields["record"]) for _, fields in store.redis.xrange(store.key("sw", "tick:steps"))]
    assert (rows[0]["step"], rows[0]["outcome"], rows[0]["budget_s"]) == ("builtins.sorted", outcome, 2.5)
    assert rows[0]["wall_s"] == pytest.approx(ended - 10.0)
    passes = store.redis.xrange(store.key("sw", "tick:passes"))
    assert json.loads(passes[0][1]["record"])["failed_steps"] == []


def test_budgeted_step_that_raises_is_an_error_not_late() -> None:
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))

    def fail() -> None:
        raise ValueError("private message")

    with timing.tick("sw", store), monotonic_clock([0.0, 9.0]):
        with pytest.raises(ValueError):
            timing.within(1.0, fail)
    record = json.loads(store.redis.xrange(store.key("sw", "tick:steps"))[0][1]["record"])
    assert (record["outcome"], record["budget_s"]) == ("error", 1.0)


def test_unbudgeted_step_record_carries_no_budget() -> None:
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    with timing.tick("sw", store), monotonic_clock([0.0, 99.0]):
        timing.call(sorted, [])
    record = json.loads(store.redis.xrange(store.key("sw", "tick:steps"))[0][1]["record"])
    assert record["outcome"] == "success"
    assert "budget_s" not in record


@pytest.mark.feature("swarm-tick-cost-report")
@pytest.mark.parametrize("outcome", ["success", "error", "stopped", "caught"])
def test_tick_pass_is_recorded_even_when_a_step_raises(outcome: str) -> None:
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    failure = tick_stop.Stopped() if outcome == "stopped" else ValueError("private message")

    @timing.instrument_tick
    def run_tick(actual: RedisStore, slug: str) -> list[str]:
        assert actual is store
        assert slug == "sw"
        try:
            with timing.step("launch"):
                if outcome != "success":
                    raise failure
        except ValueError:
            if outcome != "caught":
                raise
        return ["finished"]

    try:
        result = run_tick(store, "sw")
        assert result == ["finished"]
    except (ValueError, tick_stop.Stopped) as exc:
        assert exc is failure
    passes = store.redis.xrange(store.key("sw", "tick:passes"))
    assert len(passes) == 1
    record = json.loads(passes[0][1]["record"])
    assert record["start_ms"] <= record["end_ms"]
    assert record["wall_s"] >= 0
    assert record["agent_count"] == 0
    assert record["outcome"] == ("success" if outcome == "caught" else outcome)
    expected = [] if outcome == "success" else ["launch"]
    if outcome in ("error", "stopped"):
        expected.append("run_tick")
    assert record["failed_steps"] == expected
    assert "private message" not in passes[0][1]["record"]
    steps = [json.loads(fields["record"]) for _, fields in store.redis.xrange(store.key("sw", "tick:steps"))]
    assert [row["step"] for row in steps] == ["launch", "run_tick"]
    assert all(row["pass_id"] == record["pass_id"] for row in steps)


def test_pass_records_exact_clocks_active_agents_and_cap() -> None:
    from types import SimpleNamespace

    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    agents = [SimpleNamespace(state="working"), SimpleNamespace(state="idle"), SimpleNamespace(state="finished")]
    with (
        patch.object(timing.time, "time_ns", side_effect=[1000000000, 3000000000]),
        patch.object(timing.time, "perf_counter", side_effect=[2.0, 8.0]),
        patch.object(timing, "uuid4", return_value=SimpleNamespace(hex="identity")),
        patch.object(store, "agents", return_value=agents) as listing,
        patch.object(store.redis, "xadd", wraps=store.redis.xadd) as append,
        timing.pass_record(store, "sw"),
    ):
        current = timing.PASS.get()
        assert current is not None
        assert current["pass_id"] == "identity"
    listing.assert_called_once_with("sw")
    append.assert_called_once_with(
        store.key("sw", "tick:passes"),
        {
            "record": json.dumps(
                {
                    "pass_id": "identity",
                    "start_ms": 1000,
                    "failed_steps": [],
                    "outcome": "success",
                    "end_ms": 3000,
                    "wall_s": 6.0,
                    "agent_count": 2,
                }
            )
        },
        maxlen=50000,
        approximate=False,
    )
    assert timing.PASS.get() is None


def test_record_storage_failure_is_reported_without_masking_the_tick(capsys: pytest.CaptureFixture[str]) -> None:
    import fakeredis
    from redis.exceptions import ConnectionError

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    with patch.object(store.redis, "xadd", side_effect=ConnectionError("private connection details")):
        timing.append_record(store, "sw", "tick:steps", {"value": 1})
    assert (
        capsys.readouterr().err
        == json.dumps({"event": "swarm_tick_record_failed", "slug": "sw", "stream": "tick:steps"}) + "\n"
    )
    with patch.object(store, "agents", side_effect=ConnectionError("private connection details")):
        timing.finish_pass(store, "sw", {})
    assert (
        capsys.readouterr().err
        == json.dumps({"event": "swarm_tick_record_failed", "slug": "sw", "stream": "tick:passes"}) + "\n"
    )
