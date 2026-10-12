import pytest

from scripts.swarm import store as swarm_store
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_freezes

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def swarms(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/repo", 1, 0))
    store.update("demo", autonomy="full")
    monkeypatch.setattr(swarm_store, "connect", lambda: store)
    return store


def test_autonomy_reads_the_swarm_config(swarms):
    assert ledger_freezes.autonomy("demo") == "full"


def test_autonomy_of_a_ledger_without_a_swarm_is_empty(swarms):
    assert ledger_freezes.autonomy("no-swarm") == ""


def test_autonomy_is_empty_when_redis_fails(monkeypatch):
    from redis import RedisError

    def down():
        raise RedisError("down")

    monkeypatch.setattr(swarm_store, "connect", down)
    assert ledger_freezes.autonomy("demo") == ""
