import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime


def _store(**config):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, **config))
    return store


def test_the_tick_skips_a_swarm_on_the_event_engine():
    store, rt = _store(engine="events"), FakeRuntime()
    ledger = FakeLedger([{"id": "t1", "title": "task"}])
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == [
        "the event engine runs this swarm; the tick leaves it alone"
    ]
    assert store.redis.get(store.key("sw", "control-owner")) is None
    assert rt.spawned == []


@pytest.mark.parametrize("engine", [{}, {"engine": "legacy"}])
def test_the_tick_runs_a_swarm_on_the_legacy_engine(engine):
    store = _store(**engine)
    actions = cli.run_tick(store, "sw", FakeLedger([{"id": "t1", "title": "task"}]), FakeRuntime(), FakeHerdr({}))
    assert cli.EVENTS_ENGINE not in actions
    assert store.redis.get(store.key("sw", "control-owner")) is not None
