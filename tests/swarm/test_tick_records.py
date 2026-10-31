import json
from unittest.mock import patch

import pytest

from scripts.swarm import tick_stop, timing


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
