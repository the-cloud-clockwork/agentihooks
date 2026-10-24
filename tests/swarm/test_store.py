import json
import socket
import time

import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError


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


def test_lease_repair_is_fenced_against_a_newer_execution_and_other_holder(store):
    from dataclasses import replace

    from scripts.swarm.store import AgentRecord

    store.create(config())
    owner = AgentRecord("owner", "eng", "t1", started_at=1_000)
    store.put_agent("smoke", owner)
    assert store.ensure_claim("smoke", owner, 60_000)
    assert store.claimant("smoke", "t1") == owner.name
    store.release("smoke", "t1", owner.name)
    successor = replace(owner, started_at=2_000)
    store.put_agent("smoke", successor)
    assert not store.ensure_claim("smoke", owner, 60_000)
    assert store.claimant("smoke", "t1") is None
    assert store.claim("smoke", "t1", "other", 60_000)
    assert not store.ensure_claim("smoke", successor, 60_000)
    assert store.claimant("smoke", "t1") == "other"


def test_a_stale_release_cannot_delete_a_reacquired_lease_with_the_same_name(store):
    store.create(config())
    assert store.claim("smoke", "t1", "owner", 60_000)
    generation = store.claim_generation("smoke", "t1")
    assert store.release("smoke", "t1", "owner")
    assert store.claim("smoke", "t1", "owner", 60_000)
    assert not store.release_ready("smoke", "t1", "owner", generation, None)
    assert store.claimant("smoke", "t1") == "owner"
    assert store.release_ready("smoke", "t1", "owner", store.claim_generation("smoke", "t1"), None)


def test_ready_cleanup_compares_the_record_even_when_the_lease_did_not_change(store):
    store.create(config())
    store.claim("smoke", "t1", "owner", 60_000)
    ready_key = store.key("smoke", "ready")
    store.redis.hset(ready_key, "t1", "new-ready-record")
    generation = store.claim_generation("smoke", "t1")
    assert not store.release_ready("smoke", "t1", "owner", generation, "old-ready-record")
    assert store.claimant("smoke", "t1") == "owner"
    assert store.redis.hget(ready_key, "t1") == "new-ready-record"
    assert store.release_ready("smoke", "t1", "owner", generation, "new-ready-record")
    assert store.claimant("smoke", "t1") is None
    assert store.redis.hget(ready_key, "t1") is None


def race_at_multi(store, monkeypatch, change):
    original = store.redis.pipeline

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        multi = pipe.multi

        def begin():
            multi()
            change()

        pipe.multi = begin
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)


@pytest.mark.parametrize("kind", ["lease", "agents", "executions", "task-authority", "claim-journal"])
def test_lease_repair_rejects_a_concurrent_ownership_change(store, monkeypatch, kind):
    import json
    from dataclasses import asdict, replace

    from scripts.swarm.store import AgentRecord

    store.create(config())
    owner = AgentRecord("owner", "eng", "t1", started_at=1_000)
    store.put_agent("smoke", owner)
    row = asdict(replace(owner, started_at=2_000, execution_id="new-execution", generation=2))
    changes = {
        "lease": lambda: store.redis.set(store.key("smoke", "claim", "t1"), "successor"),
        "agents": lambda: store.redis.hset(
            store.key("smoke", "agents"), owner.name, json.dumps(asdict(replace(owner, started_at=2_000)))
        ),
        "executions": lambda: store.redis.hset(store.key("smoke", "executions"), "new-execution", json.dumps(row)),
        "task-authority": lambda: store.redis.set(store.key("smoke", "task-authority", "t1"), "new-authority"),
        "claim-journal": lambda: store.redis.set(store.key("smoke", "claim-journal", "t1"), "new-journal"),
    }
    race_at_multi(store, monkeypatch, changes[kind])
    assert not store.ensure_claim("smoke", owner, 60_000)
    assert store.claimant("smoke", "t1") == ("successor" if kind == "lease" else None)


@pytest.mark.parametrize("kind", ["lease", "generation", "ready", "task-authority", "claim-journal"])
def test_ready_cleanup_rejects_a_concurrent_ownership_change(store, monkeypatch, kind):
    store.create(config())
    store.claim("smoke", "t1", "owner", 60_000)
    key, ready_key = store.key("smoke", "claim", "t1"), store.key("smoke", "ready")
    generation = store.claim_generation("smoke", "t1")
    store.redis.hset(ready_key, "t1", "old-ready")
    changes = {
        "lease": lambda: store.redis.set(key, "successor"),
        "generation": lambda: store.redis.incr(f"{key}:generation"),
        "ready": lambda: store.redis.hset(ready_key, "t1", "successor-ready"),
        "task-authority": lambda: store.redis.set(store.key("smoke", "task-authority", "t1"), "new-authority"),
        "claim-journal": lambda: store.redis.set(store.key("smoke", "claim-journal", "t1"), "new-journal"),
    }
    race_at_multi(store, monkeypatch, changes[kind])
    assert not store.release_ready("smoke", "t1", "owner", generation, "old-ready")
    assert store.claimant("smoke", "t1") == ("successor" if kind == "lease" else "owner")
    assert store.redis.hget(ready_key, "t1") == ("successor-ready" if kind == "ready" else "old-ready")


@pytest.mark.parametrize("state", ["starting", "working", "awaiting-decision"])
def test_lease_repair_preserves_its_generation_and_expiry_until_reacquired(store, state):
    from scripts.swarm.store import AgentRecord

    store.create(config())
    owner = AgentRecord("owner", "eng", "t1", state=state)
    assert not store.ensure_claim("smoke", owner, 60_000)
    store.put_agent("smoke", owner)
    assert store.ensure_claim("smoke", owner, 60_000)
    assert store.claim_generation("smoke", "t1") == 1
    assert 0 < store.redis.pttl(store.key("smoke", "claim", "t1")) <= 60_000
    assert store.ensure_claim("smoke", owner, 60_000)
    assert store.claim_generation("smoke", "t1") == 1
    store.release("smoke", "t1", owner.name)
    assert store.ensure_claim("smoke", owner, 60_000)
    assert store.claim_generation("smoke", "t1") == 2


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


def test_a_held_claim_refund_gives_back_one_life_only_while_claimant_and_generation_match(store):
    store.record_launch("smoke", AgentRecord("first", "eng", "t1"), "started")
    store.record_launch("smoke", AgentRecord("second", "eng", "t1"), "started")
    store.claim("smoke", "t1", "second", 60_000)
    generation = store.claim_generation("smoke", "t1")

    assert store.refund_held_claim("smoke", "t1", "first", generation) is False
    assert store.refund_held_claim("smoke", "t1", "second", generation + 1) is False
    assert store.claims("smoke", "t1") == 2
    assert store.refund_held_claim("smoke", "t1", "second", generation) is True
    assert store.claims("smoke", "t1") == 1
    assert store.claimant("smoke", "t1") == "second"


def test_a_held_claim_refunds_one_life_however_many_paths_ask(store):
    for _ in range(3):
        store.count_claim("smoke", "t1")
    store.claim("smoke", "t1", "agent", 60_000)
    generation = store.claim_generation("smoke", "t1")

    assert store.refund_held_claim("smoke", "t1", "agent", generation) is True
    assert store.refund_held_claim("smoke", "t1", "agent", generation) is False
    assert store.claims("smoke", "t1") == 2
    assert store.claimant("smoke", "t1") == "agent"


def test_a_held_claim_refund_reads_a_missing_generation_as_zero(store):
    store.count_claim("smoke", "t1")
    store.redis.set(store.key("smoke", "claim", "t1"), "agent")

    assert store.refund_held_claim("smoke", "t1", "agent", 1) is False
    assert store.refund_held_claim("smoke", "t1", "agent", 0) is True
    assert store.claims("smoke", "t1") == 0


def test_a_held_claim_refund_refuses_after_the_hub_released_the_claim(store):
    store.count_claim("smoke", "t1")
    store.claim("smoke", "t1", "agent", 60_000)
    generation = store.claim_generation("smoke", "t1")
    store.release("smoke", "t1", "agent")

    assert store.refund_held_claim("smoke", "t1", "agent", generation) is False
    assert store.claims("smoke", "t1") == 1


def test_a_held_claim_refund_refuses_after_the_same_claimant_claimed_again(store):
    store.count_claim("smoke", "t1")
    store.claim("smoke", "t1", "agent", 60_000)
    generation = store.claim_generation("smoke", "t1")
    store.release("smoke", "t1", "agent")
    store.claim("smoke", "t1", "agent", 60_000)

    assert store.refund_held_claim("smoke", "t1", "agent", generation) is False
    assert store.claims("smoke", "t1") == 1


def test_a_held_claim_refund_refuses_a_second_refund_for_the_same_claimant_and_generation(store):
    for _ in range(3):
        store.count_claim("smoke", "t1")
    store.count_claim("smoke", "t2")
    store.claim("smoke", "t1", "agent", 60_000)
    store.claim("smoke", "t2", "agent", 60_000)
    generation = store.claim_generation("smoke", "t1")

    assert store.refund_held_claim("smoke", "t1", "agent", generation) is True
    assert store.refund_held_claim("smoke", "t1", "agent", generation) is False
    assert store.claimant("smoke", "t1") == "agent"
    assert store.claims("smoke", "t1") == 2
    assert store.refund_held_claim("smoke", "t2", "agent", store.claim_generation("smoke", "t2")) is True
    store.release("smoke", "t1", "agent")
    store.claim("smoke", "t1", "agent", 60_000)
    assert store.refund_held_claim("smoke", "t1", "agent", generation + 1) is True
    assert store.claims("smoke", "t1") == 1


@pytest.mark.parametrize(
    ("moved_key", "move", "lives"),
    [
        ("claim", lambda redis, key: redis.set(key, "other"), 1),
        ("claim", lambda redis, key: redis.incr(f"{key}:generation"), 1),
        ("claim", lambda redis, key: redis.set(f"{key}:refunded", redis.get(f"{key}:generation")), 1),
        ("started-lives", lambda redis, key: redis.hset(key, "t1", 5), 5),
        ("refunded-claims", lambda redis, key: redis.hset(key, "t1", "agent:1"), 1),
        ("task-authority", lambda redis, key: redis.set(key, "hub"), 1),
        ("claim-journal", lambda redis, key: redis.set(key, "hub"), 1),
    ],
)
def test_a_held_claim_refund_refuses_when_a_watched_key_moves_during_the_watch(
    store, monkeypatch, moved_key, move, lives
):
    store.count_claim("smoke", "t1")
    store.claim("smoke", "t1", "agent", 60_000)
    generation = store.claim_generation("smoke", "t1")
    started = store._started_lives
    hashes = ("started-lives", "refunded-claims")
    key = store.key("smoke", moved_key) if moved_key in hashes else store.key("smoke", moved_key, "t1")

    def moved(slug, task):
        move(store.redis, key)
        return started(slug, task)

    monkeypatch.setattr(store, "_started_lives", moved)

    assert store.refund_held_claim("smoke", "t1", "agent", generation) is False
    assert store.claims("smoke", "t1") == lives


@pytest.mark.parametrize("guard", ["task-authority", "claim-journal"])
def test_a_held_claim_refund_refuses_while_a_claim_guard_is_set(store, guard):
    store.count_claim("smoke", "t1")
    store.claim("smoke", "t1", "agent", 60_000)
    generation = store.claim_generation("smoke", "t1")
    store.redis.set(store.key("smoke", guard, "t1"), "hub")

    assert store.refund_held_claim("smoke", "t1", "agent", generation) is False
    assert store.claims("smoke", "t1") == 1


def test_a_held_claim_refund_refuses_when_no_started_life_is_left(store):
    store.claim("smoke", "t1", "agent", 60_000)
    generation = store.claim_generation("smoke", "t1")
    store.redis.hset(store.key("smoke", "started-lives"), "t1", 0)

    assert store.refund_held_claim("smoke", "t1", "agent", generation) is False
    assert store.redis.hget(store.key("smoke", "started-lives"), "t1") == "0"


def test_a_held_claim_refund_retries_after_another_task_moved_the_lives_hash(store, monkeypatch):
    store.count_claim("smoke", "t1")
    store.claim("smoke", "t1", "agent", 60_000)
    generation = store.claim_generation("smoke", "t1")
    started, calls = store._started_lives, []

    def moved(slug, task):
        calls.append(task)
        if len(calls) == 1:
            store.redis.hincrby(store.key("smoke", "started-lives"), "t2", 1)
        return started(slug, task)

    monkeypatch.setattr(store, "_started_lives", moved)

    assert store.refund_held_claim("smoke", "t1", "agent", generation) is True
    assert (len(calls), store.claims("smoke", "t1"), store.claims("smoke", "t2")) == (2, 0, 1)


def test_a_held_claim_refund_gives_up_after_three_watch_conflicts(store, monkeypatch):
    store.count_claim("smoke", "t1")
    store.claim("smoke", "t1", "agent", 60_000)
    generation = store.claim_generation("smoke", "t1")
    started, calls = store._started_lives, []

    def moved(slug, task):
        calls.append(task)
        store.redis.hincrby(store.key("smoke", "started-lives"), "t2", 1)
        return started(slug, task)

    monkeypatch.setattr(store, "_started_lives", moved)

    assert store.refund_held_claim("smoke", "t1", "agent", generation) is False
    assert (len(calls), store.claims("smoke", "t1")) == (3, 1)


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
        {
            "agent": "waiting",
            "task": "t3",
            "at": 4,
            "state": "pending",
            "error": "",
            "pane_id": "",
            "after": "",
            "head": "",
        },
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


def refused_unreachable_redis():
    from scripts.swarm.store import connect

    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        start = time.monotonic()
        with pytest.raises(SwarmError, match="refuses to run"):
            connect({"AGENTIHOOKS_SWARM_REDIS_URL": f"redis://127.0.0.1:{closed.getsockname()[1]}/0"})
    return time.monotonic() - start


def refused_suite_redis():
    from scripts.swarm.store import connect

    start = time.monotonic()
    with pytest.raises(SwarmError, match="refuses to run"):
        connect()
    return time.monotonic() - start


def test_an_unreachable_redis_is_refused_with_a_clear_error():
    refused_unreachable_redis()


@pytest.mark.wall_clock
def test_an_unreachable_redis_is_refused_within_a_second():
    assert refused_unreachable_redis() < 1


def test_the_suite_swarm_redis_is_refused():
    refused_suite_redis()


@pytest.mark.wall_clock
def test_the_suite_swarm_redis_is_refused_at_once():
    assert refused_suite_redis() < 1


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


def test_the_swap_use_share_round_trips_and_an_old_config_reads_the_default(store):
    from scripts.swarm.store import DEFAULT_SWAP_FULL_SHARE

    store.create(config(swap_full_share=0.75))
    assert store.config("smoke").swap_full_share == 0.75
    assert store.update("smoke", swap_full_share=1).swap_full_share == 1
    store.redis.hdel(store.key("smoke", "config"), "swap_full_share")
    assert store.config("smoke").swap_full_share == DEFAULT_SWAP_FULL_SHARE == 0.9


def test_the_swap_floor_round_trips_and_an_old_config_reads_the_default(store):
    from scripts.swarm.store import DEFAULT_SWAP_FLOOR_MB

    store.create(config(swap_floor_mb=3000))
    assert store.config("smoke").swap_floor_mb == 3000
    assert store.update("smoke", swap_floor_mb=1).swap_floor_mb == 1
    store.redis.hdel(store.key("smoke", "config"), "swap_floor_mb")
    assert store.config("smoke").swap_floor_mb == DEFAULT_SWAP_FLOOR_MB == 4096


def test_create_refuses_a_swap_use_share_outside_zero_to_one(store):
    with pytest.raises(SwarmError) as caught:
        store.create(config(swap_full_share=1.5))
    assert str(caught.value) == "swap full share must be above 0 and at most 1"


def test_the_api_address_round_trips_and_an_old_config_reads_it_empty(store):
    store.create(config(api_url="https://swarm.example.test:8443"))
    assert store.config("smoke").api_url == "https://swarm.example.test:8443"
    store.redis.hdel(store.key("smoke", "config"), "api_url")
    assert store.config("smoke").api_url == ""


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
        {"load_low": True},
        {"load_high": True},
        {"load_high": "2"},
        {"swap_full_share": 0},
        {"swap_full_share": 1.01},
        {"swap_full_share": True},
        {"swap_full_share": "0.5"},
        {"swap_floor_mb": 0},
        {"swap_floor_mb": 0.5},
        {"swap_floor_mb": True},
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
    with pytest.raises(SwarmError) as caught:
        store.create(config(memory_per_agent_mb=memory))
    assert str(caught.value) == "memory per agent must be a whole number of MB above 0"
    assert store.redis.hgetall(store.key("smoke", "config")) == {}
    assert store.slugs() == []


def test_scaling_boundaries_round_trip_and_manual_preserves_lane_caps(store):
    store.create(config())
    stored = store.update("smoke", scaling="manual", load_low=10, load_high=10, memory_per_agent_mb=1)
    assert store.config("smoke") == stored
    assert (stored.max_eng, stored.max_ci, stored.max_plan) == (2, 1, 1)


def test_scaling_update_explains_the_allowed_modes(store):
    store.create(config())
    with pytest.raises(SwarmError) as caught:
        store.update("smoke", scaling="sometimes")
    assert str(caught.value) == "scaling must be one of auto, manual"
