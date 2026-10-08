"""Swarm Redis stores in the suite write under the suite's key prefix, and a write to a production key name fails."""

import fakeredis
import pytest

from scripts import session_caps
from scripts.gates.progress import Progress
from scripts.inbox.seen import SeenMarks
from scripts.inbox.store import InboxStore
from scripts.swarm import store as swarm_store
from scripts.swarm.store import RedisStore, SwarmConfig
from tests import redis_key_guard, swarm_v2_isolation

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def redis(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(swarm_store, "redis_client", lambda environ=None: client)
    return client


def test_every_swarm_store_writes_under_the_suite_prefix(redis):
    store = RedisStore(redis)
    store.create(SwarmConfig(slug="demo", repo="/repo", max_eng=1, max_ci=0))
    store.seats.occupy("eng-1@demo", "engineer@100001-0001", 1)
    store.memory.learn("eng-1@demo", "engineer@100001-0001", "a lesson because a reason", 1)
    store.culture.set("demo", "culture text")
    InboxStore(redis).send("master@demo", "eng-1@demo", "hello")
    Progress(redis, "demo").outcome("eng-1@demo", "pushed")
    SeenMarks(redis).mark("eng-1@demo", "demo:1:c1")
    session_caps.set_cap("acct", 2)

    keys = sorted(redis.scan_iter("*"))

    assert len(keys) > 5
    assert [key for key in keys if not key.startswith(f"{swarm_v2_isolation.RUN_PREFIX}:")] == []


def test_a_production_key_write_fails_the_test():
    client = fakeredis.FakeRedis(decode_responses=True)
    before = len(redis_key_guard.written)

    with pytest.raises(redis_key_guard.ProductionKey) as refused:
        client.hset("agentihooks:swarm:demo:config", "slug", "demo")
    caught = redis_key_guard.written[before:]
    del redis_key_guard.written[before:]

    assert str(refused.value) == "a test wrote the production Redis key agentihooks:swarm:demo:config"
    assert caught == ["agentihooks:swarm:demo:config"]
    assert client.exists("agentihooks:swarm:demo:config") == 0


def test_a_queued_pipeline_write_to_a_production_key_fails_the_test():
    client = fakeredis.FakeRedis(decode_responses=True)
    before = len(redis_key_guard.written)

    with pytest.raises(redis_key_guard.ProductionKey):
        client.pipeline().set("ok", "1").delete("agentihooks:inbox:item:1")
    caught = redis_key_guard.written[before:]
    del redis_key_guard.written[before:]

    assert caught == ["agentihooks:inbox:item:1"]


def test_reads_and_suite_prefixed_writes_pass():
    client = fakeredis.FakeRedis(decode_responses=True)
    before = len(redis_key_guard.written)

    client.get("agentihooks:swarm:demo:config")
    client.set(f"{swarm_v2_isolation.RUN_PREFIX}:swarm:demo:config", "agentihooks:swarm:demo")

    assert redis_key_guard.written[before:] == []
