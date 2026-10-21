import pytest

from scripts.inbox import exits
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, tasks


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    return s


def _dropping(drops):
    from redis.exceptions import ConnectionError as RedisConnectionError

    calls = []

    def sweep(*args):
        calls.append(args)
        if len(calls) <= drops:
            raise RedisConnectionError("Connection closed by server.")

    sweep.__module__, sweep.__qualname__ = exits.__name__, "sweep"
    return sweep, calls


def test_a_dropped_connection_in_the_exit_sweep_is_retried_once(store, monkeypatch):
    sweep, calls = _dropping(1)
    monkeypatch.setattr(exits, "sweep", sweep)
    runtime = FakeRuntime()
    tick("sw", store, tasks(("t1", "eng")), runtime, 1)
    assert len(calls) == 2
    assert [task for _, _, task in runtime.spawned] == ["t1"]


def test_an_exit_sweep_that_keeps_dropping_logs_and_the_tick_still_spawns(store, monkeypatch, capsys):
    sweep, calls = _dropping(2)
    monkeypatch.setattr(exits, "sweep", sweep)
    runtime = FakeRuntime()
    actions = tick("sw", store, tasks(("t1", "eng")), runtime, 1)
    assert len(calls) == 2
    assert [task for _, _, task in runtime.spawned] == ["t1"]
    assert (
        "scripts.inbox.exits.sweep skipped, its Redis connection dropped twice: Connection closed by server."
        in capsys.readouterr().err
    )
    assert "skipped scripts.inbox.exits.sweep: its Redis connection dropped twice" in actions
