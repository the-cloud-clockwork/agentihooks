import pytest

from scripts.handoff import transfers
from scripts.swarm import tick as tick_module
from scripts.swarm import timing
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import skip_refused, tick
from tests.swarm.test_tick import FakeRuntime, tasks


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    return s


class TimingOutRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def conversations(self):
        from redis.exceptions import TimeoutError as RedisTimeoutError

        self.reads += 1
        raise RedisTimeoutError("Timeout reading from socket")


def test_a_redis_timeout_in_the_conversations_step_lets_the_pass_finish(store, monkeypatch, capsys):
    observed, models, closed, failures = [], [], [], []
    monkeypatch.setattr(transfers, "observe", lambda *args: observed.append(args[1]))
    real_models = tick_module._session_models
    monkeypatch.setattr(tick_module, "_session_models", lambda *args: models.append(args) or real_models(*args))
    real_close = tick_module._close_space
    monkeypatch.setattr(tick_module, "_close_space", lambda *args: closed.append(args[0]) or real_close(*args))
    runtime = TimingOutRuntime()
    token = timing.ON_FAILURE.set(lambda name, exc: failures.append((name, type(exc).__name__)))
    try:
        with timing.tick("sw"):
            actions = tick("sw", store, tasks(("t1", "eng")), runtime, 1)
    finally:
        timing.ON_FAILURE.reset(token)
    assert runtime.reads == 1
    assert [task for _, _, task in runtime.spawned] == ["t1"]
    assert observed == ["sw"]
    assert len(models) == 1
    assert closed == ["sw"]
    assert failures == [("scripts.swarm.tick._conversations", "TimeoutError")]
    assert "skipped scripts.swarm.tick._conversations: its Redis read timed out" in actions
    assert (
        "scripts.swarm.tick._conversations skipped, its Redis read timed out: Timeout reading from socket"
        in capsys.readouterr().err
    )


def test_a_step_that_times_out_returns_its_skipped_action():
    from redis.exceptions import TimeoutError as RedisTimeoutError

    def step():
        raise RedisTimeoutError("slow")

    assert skip_refused(step) == [f"skipped {__name__}.{step.__qualname__}: its Redis read timed out"]


def test_a_dropped_connection_still_raises_from_a_plain_step():
    from redis.exceptions import ConnectionError as RedisConnectionError

    def step():
        raise RedisConnectionError("gone")

    with pytest.raises(RedisConnectionError):
        skip_refused(step)
