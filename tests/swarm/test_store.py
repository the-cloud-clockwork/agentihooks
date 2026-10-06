import socket
import time

import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def config(**kw):
    return SwarmConfig(**{"slug": "smoke", "repo": "/repo", "max_eng": 2, "max_ci": 1, **kw})


def test_create_then_read_config_and_list(store):
    store.create(config())
    assert store.config("smoke") == config(state="running", code="a1b2c3", name="swarm@a1b2c3")
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
    assert store.claim("smoke", "t1", "engineer@a1b2c3-0001", lease_ms=60_000)
    assert not store.claim("smoke", "t1", "engineer@a1b2c3-0002", lease_ms=60_000)
    assert store.claimant("smoke", "t1") == "engineer@a1b2c3-0001"
    store.release("smoke", "t1", "engineer@a1b2c3-0002")
    assert store.claimant("smoke", "t1") == "engineer@a1b2c3-0001"
    store.release("smoke", "t1", "engineer@a1b2c3-0001")
    assert store.claim("smoke", "t1", "engineer@a1b2c3-0002", lease_ms=60_000)


def test_claims_count_agent_lives_per_task_until_reset(store):
    store.create(config())
    assert store.claims("smoke", "t1") == 0
    assert [store.count_claim("smoke", "t1") for _ in range(3)] == [1, 2, 3]
    store.count_claim("smoke", "t2")
    store.reset_claims("smoke", "t1")
    assert (store.claims("smoke", "t1"), store.claims("smoke", "t2")) == (0, 1)


def test_a_lapsed_lease_frees_the_claim(store):
    store.create(config())
    store.claim("smoke", "t1", "engineer@a1b2c3-0001", lease_ms=60_000)
    store.redis.delete(store.key("smoke", "claim", "t1"))
    assert store.claimant("smoke", "t1") is None
    assert store.refresh("smoke", "t1", "engineer@a1b2c3-0001", lease_ms=60_000) is False


def test_agent_registry_and_sequence(store):
    store.create(config())
    assert [store.next_name("smoke", "eng") for _ in range(2)] == ["engineer@a1b2c3-0001", "engineer@a1b2c3-0002"]
    agent = AgentRecord(
        name="engineer@a1b2c3-0001", lane="eng", task="t1", pane_id="w1:p1", harness="claude", started_at=5
    )
    store.put_agent("smoke", agent)
    store.put_agent("smoke", AgentRecord(**{**agent.__dict__, "state": "finished"}))
    assert store.agents("smoke") == [AgentRecord(**{**agent.__dict__, "state": "finished"})]
    store.drop_agent("smoke", "engineer@a1b2c3-0001")
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


def test_the_suite_swarm_redis_is_refused_at_once():
    from scripts.swarm.store import connect

    start = time.monotonic()
    with pytest.raises(SwarmError, match="refuses to run"):
        connect()
    assert time.monotonic() - start < 1


def test_one_redis_for_every_caller_whatever_redis_url_says():
    from scripts.swarm.store import DEFAULT_URL, redis_url

    assert redis_url({"AGENTIHOOKS_SWARM_REDIS_URL": "redis://a", "REDIS_URL": "redis://b"}) == "redis://a"
    assert redis_url({"REDIS_URL": "redis://b"}) == DEFAULT_URL


def test_compact_limit_round_trips_and_an_old_config_reads_as_unset(store):
    store.create(config(compact_limit=40))
    assert store.config("smoke").compact_limit == 40
    store.redis.hdel(store.key("smoke", "config"), "compact_limit")
    assert store.config("smoke").compact_limit == 0


def test_template_and_lane_map_round_trip_and_an_old_config_reads_as_none(store):
    lanes = {"eng": {"role": "r", "agent": "codex", "model": "m", "effort": "high", "kind": "code"}}
    store.create(config(template="t", lanes=lanes))
    assert (store.config("smoke").template, store.config("smoke").lanes) == ("t", lanes)
    store.redis.hdel(store.key("smoke", "config"), "template", "lanes")
    assert (store.config("smoke").template, store.config("smoke").lanes) == ("", {})


def test_links_round_trip_and_an_old_config_reads_as_none(store):
    links = [{"from": "eng", "to": "ci", "kind": "delegates-to"}]
    store.create(config(links=links))
    assert store.config("smoke").links == links
    store.redis.hdel(store.key("smoke", "config"), "links")
    assert store.config("smoke").links == []
