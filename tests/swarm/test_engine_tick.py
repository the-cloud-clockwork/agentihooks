from scripts.swarm import cli
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime


def _store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("events", "/repo", max_eng=1, max_ci=0, engine="events"))
    store.create(SwarmConfig("legacy", "/repo", max_eng=1, max_ci=0))
    return store


def test_the_tick_skips_a_swarm_on_the_event_engine_and_runs_one_on_the_legacy_engine():
    store = _store()
    ledger, rt = FakeLedger([{"id": "t1", "title": "task"}]), FakeRuntime()
    assert cli.run_tick(store, "events", ledger, rt, FakeHerdr({})) == [
        "the event engine runs this swarm; the tick leaves it alone"
    ]
    assert store.redis.get(store.key("events", "control-owner")) is None
    assert rt.spawned == []
    cli.run_tick(store, "legacy", ledger, rt, FakeHerdr({}))
    assert store.redis.get(store.key("legacy", "control-owner")) is not None
