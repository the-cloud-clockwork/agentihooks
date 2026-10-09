import json
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
    assert store.config("smoke") == config(state="running", code="a1b2c3")
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
    store.note_launch_failure("smoke", "t1", "herdr down")
    store.note_launch_failure("smoke", "t2", "canary timeout")
    assert (store.launch_failure("smoke", "t1"), store.launch_failure("smoke", "t3")) == ("herdr down", "")
    store.reset_claims("smoke", "t1")
    assert (store.claims("smoke", "t1"), store.claims("smoke", "t2")) == (0, 1)
    assert (store.launch_failure("smoke", "t1"), store.launch_failure("smoke", "t2")) == ("", "canary timeout")


def test_legacy_claim_counts_do_not_prove_started_lives(store):
    store.redis.hset(store.key("smoke", "claims"), "t1", 5)
    assert store.claims("smoke", "t1") == 0
    store.count_claim("smoke", "t1")
    assert store.claims("smoke", "t1") == 1
    assert store.redis.hget(store.key("smoke", "claims"), "t1") == "5"


def test_verified_historical_starts_keep_their_life_budget(store):
    failed = AgentRecord("failed", "eng", "t1", state="starting")
    started = AgentRecord(
        "started",
        "eng",
        "t1",
        started_at=1,
        profile_decision={"validation": {"state": "validated"}},
    )
    store.put_agent("smoke", failed)
    store.drop_agent("smoke", failed.name)
    store.put_agent("smoke", started)
    assert store.claims("smoke", "t1") == 1
    store.drop_agent("smoke", started.name)
    assert store.claims("smoke", "t1") == 1
    assert [(row["agent"], row["state"]) for row in store.launches("smoke")] == [("started", "started")]
    store.count_claim("smoke", "t1")
    store.record_launch("smoke", AgentRecord("next", "eng", "t1"), "started")
    assert store.claims("smoke", "t1") == 2
    store.reset_claims("smoke", "t1")
    assert store.claims("smoke", "t1") == 0


def test_a_refunded_claim_gives_back_one_started_life(store):
    store.record_launch("smoke", AgentRecord("first", "eng", "t1"), "started")
    store.record_launch("smoke", AgentRecord("second", "eng", "t1"), "started")
    assert store.refund_claim("smoke", "t1") == 1
    assert store.claims("smoke", "t1") == 1
    store.count_claim("smoke", "t2")
    assert (store.refund_claim("smoke", "t2"), store.claims("smoke", "t2")) == (0, 0)


def test_launch_rows_merge_recorded_launches_with_verified_history(store):
    history, validated = store.key("smoke", "history"), {"validation": {"state": "validated"}}
    store.redis.rpush(
        history, json.dumps({"name": "first", "task": "t1", "started_at": 1, "profile_decision": validated})
    )
    store.redis.rpush(history, json.dumps({"name": "legacy", "task": "t1", "started_at": 2}))
    store.redis.rpush(
        history, json.dumps({"name": "last", "task": "t2", "started_at": 3, "profile_decision": validated})
    )
    store.record_launch("smoke", AgentRecord("waiting", "eng", "t3", started_at=4), "pending")
    assert sorted(store.launches("smoke"), key=lambda row: row["agent"]) == [
        {"agent": "first", "task": "t1", "at": 1, "state": "started", "error": ""},
        {"agent": "last", "task": "t2", "at": 3, "state": "started", "error": ""},
        {"agent": "waiting", "task": "t3", "at": 4, "state": "pending", "error": ""},
    ]


def test_earlier_lives_are_the_tasks_worker_lives_newest_first(store):
    history = store.key("smoke", "history")
    for row in (
        {"name": "first", "task": "t1", "lane": "eng", "ended_at": 5},
        {"name": "unended", "task": "t1", "lane": "eng"},
        {"name": "early", "task": "t1", "lane": "eng", "ended_at": 1},
        {"name": "master", "task": "t1", "lane": "master", "ended_at": 9},
        {"name": "other", "task": "t2", "lane": "eng", "ended_at": 7},
        {"name": "last", "task": "t1", "lane": "eng", "ended_at": 8},
    ):
        store.redis.rpush(history, json.dumps(row))
    assert store.earlier_lives("smoke", "t1") == ["last", "first", "early", "unended"]
    assert store.earlier_lives("smoke", "t3") == []


def test_a_reclaim_verdict_is_kept_per_claimant(store):
    store.put_reclaim("smoke", "eng-2", {"continue_from": "fresh"})
    store.put_reclaim("smoke", "eng-3", {"continue_from": "origin/b"})
    assert store.reclaims("smoke") == {"eng-2": {"continue_from": "fresh"}, "eng-3": {"continue_from": "origin/b"}}
    assert store.reclaims("other") == {}


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


def test_scaling_settings_round_trip_and_an_old_config_reads_the_defaults(store):
    from scripts.swarm.store import DEFAULT_LOAD_HIGH, DEFAULT_LOAD_LOW, DEFAULT_MEMORY_PER_AGENT_MB

    store.create(config(scaling="manual", load_high=1.5, load_low=0.5, memory_per_agent_mb=900))
    read = store.config("smoke")
    assert (read.scaling, read.load_high, read.load_low, read.memory_per_agent_mb) == ("manual", 1.5, 0.5, 900)
    store.redis.hdel(store.key("smoke", "config"), "scaling", "load_high", "load_low", "memory_per_agent_mb")
    read = store.config("smoke")
    assert (read.scaling, read.load_high, read.load_low, read.memory_per_agent_mb) == (
        "auto",
        DEFAULT_LOAD_HIGH,
        DEFAULT_LOAD_LOW,
        DEFAULT_MEMORY_PER_AGENT_MB,
    )
    assert DEFAULT_LOAD_LOW < DEFAULT_LOAD_HIGH


@pytest.mark.parametrize(
    "changes",
    [
        {"load_low": 2.0, "load_high": 1.0},
        {"load_low": 5.0},
        {"scaling": "sometimes"},
        {"memory_per_agent_mb": 0},
        {"memory_per_agent_mb": 0.5},
        {"memory_per_agent_mb": True},
        {"load_high": 0.0, "load_low": 0.0},
    ],
)
def test_update_refuses_bad_scaling_settings_and_keeps_the_stored_ones(store, changes):
    store.create(config())
    before = store.config("smoke")
    with pytest.raises(SwarmError):
        store.update("smoke", **changes)
    assert store.config("smoke") == before


def test_create_refuses_a_low_watermark_above_the_high_one(store):
    with pytest.raises(SwarmError, match="load low"):
        store.create(config(load_low=2.0, load_high=1.0))


@pytest.mark.parametrize("memory", [0.5, True])
def test_create_refuses_memory_that_cannot_round_trip(store, memory):
    with pytest.raises(SwarmError, match="whole number"):
        store.create(config(memory_per_agent_mb=memory))
    assert store.redis.hgetall(store.key("smoke", "config")) == {}
    assert store.slugs() == []


def test_scaling_boundaries_round_trip_and_manual_preserves_lane_caps(store):
    store.create(config())
    stored = store.update("smoke", scaling="manual", load_low=10.0, load_high=10.0, memory_per_agent_mb=1)
    assert store.config("smoke") == stored
    assert (stored.max_eng, stored.max_ci, stored.max_plan) == (2, 1, 1)


def test_scaling_update_explains_the_allowed_modes(store):
    store.create(config())
    with pytest.raises(SwarmError) as caught:
        store.update("smoke", scaling="sometimes")
    assert str(caught.value) == "scaling must be one of auto, manual"
