import socket
import time

import fakeredis
import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError


@pytest.fixture
def store():
    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def config(**kw):
    return SwarmConfig(**{"slug": "smoke", "repo": "/repo", "max_eng": 2, "max_ci": 1, **kw})


def test_create_then_read_config_and_list(store):
    store.create(config())
    assert store.config("smoke") == config(state="running")
    assert store.slugs() == ["smoke"]


def test_create_twice_is_refused(store):
    store.create(config())
    with pytest.raises(SwarmError):
        store.create(config())


def test_set_updates_caps_and_state(store):
    store.create(config())
    store.update("smoke", max_eng=3, state="paused")
    assert (store.config("smoke").max_eng, store.config("smoke").state) == (3, "paused")


def test_a_task_claim_is_exclusive_until_released(store):
    store.create(config())
    assert store.claim("smoke", "t1", "smoke-eng-1", lease_ms=60_000)
    assert not store.claim("smoke", "t1", "smoke-eng-2", lease_ms=60_000)
    assert store.claimant("smoke", "t1") == "smoke-eng-1"
    store.release("smoke", "t1", "smoke-eng-2")
    assert store.claimant("smoke", "t1") == "smoke-eng-1"
    store.release("smoke", "t1", "smoke-eng-1")
    assert store.claim("smoke", "t1", "smoke-eng-2", lease_ms=60_000)


def test_a_lapsed_lease_frees_the_claim(store):
    store.create(config())
    store.claim("smoke", "t1", "smoke-eng-1", lease_ms=60_000)
    store.redis.delete(store.key("smoke", "claim", "t1"))
    assert store.claimant("smoke", "t1") is None
    assert store.refresh("smoke", "t1", "smoke-eng-1", lease_ms=60_000) is False


def test_agent_registry_and_sequence(store):
    store.create(config())
    assert [store.next_name("smoke", "eng") for _ in range(2)] == ["smoke-eng-1", "smoke-eng-2"]
    agent = AgentRecord(name="smoke-eng-1", lane="eng", task="t1", pane_id="w1:p1", harness="claude", started_at=5)
    store.put_agent("smoke", agent)
    store.put_agent("smoke", AgentRecord(**{**agent.__dict__, "state": "finished"}))
    assert store.agents("smoke") == [AgentRecord(**{**agent.__dict__, "state": "finished"})]
    store.drop_agent("smoke", "smoke-eng-1")
    assert store.agents("smoke") == []


def test_no_redis_refuses():
    with pytest.raises(SwarmError):
        RedisStore(None)


def test_an_unreachable_redis_is_refused_with_a_clear_error():
    from scripts.swarm.store import connect

    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        start = time.monotonic()
        with pytest.raises(SwarmError, match="refuses to run"):
            connect({"AGENTIHOOKS_SWARM_REDIS_URL": f"redis://127.0.0.1:{closed.getsockname()[1]}/0"})
    assert time.monotonic() - start < 1


def test_one_redis_for_every_caller_whatever_redis_url_says():
    from scripts.swarm.store import DEFAULT_URL, redis_url

    assert redis_url({"AGENTIHOOKS_SWARM_REDIS_URL": "redis://a", "REDIS_URL": "redis://b"}) == "redis://a"
    assert redis_url({"REDIS_URL": "redis://b"}) == DEFAULT_URL
