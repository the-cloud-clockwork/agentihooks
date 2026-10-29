"""The store family readers the account session readers call."""


def test_transport_records_need_a_store(monkeypatch):
    from scripts.codex_session import binding

    monkeypatch.setattr("hooks._redis.get_redis", lambda: None)

    assert binding.transport_records() is None


def test_transport_records_are_every_swarms_raw_records(monkeypatch):
    import fakeredis

    from scripts.codex_session import binding
    from scripts.swarm.store import PREFIX, RedisStore

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("hooks._redis.get_redis", lambda: redis)
    redis.sadd(f"{PREFIX}:index", "one", "two")
    store = RedisStore(redis)
    redis.hset(store.key("one", binding.RECORDS), "exe-1", "raw-1")
    redis.hset(store.key("two", binding.RECORDS), "exe-2", "raw-2")
    redis.hset(store.key("two", binding.ENDPOINTS), "exe-3", "not-a-record")

    assert binding.transport_records() == ["raw-1", "raw-2"]


def test_fleet_held_now_needs_a_store(monkeypatch):
    from scripts.swarm_v2 import accounts

    monkeypatch.setattr("hooks._redis.get_redis", lambda: None)

    assert accounts.fleet_held_now() == {}


def test_fleet_held_now_reads_the_account_slots_at_the_store_clock(monkeypatch):
    import fakeredis

    from scripts.swarm_v2 import accounts

    redis = fakeredis.FakeRedis(decode_responses=True)
    seen = []

    def held(store, now):
        seen.append((store.redis, now))
        return {"alpha": 1}

    monkeypatch.setattr("hooks._redis.get_redis", lambda: redis)
    monkeypatch.setattr(accounts.lease, "now_ms", lambda store: 1234 if store.redis is redis else 0)
    monkeypatch.setattr(accounts, "fleet_held", held)

    assert accounts.fleet_held_now() == {"alpha": 1}
    assert seen == [(redis, 1234)]


def test_held_elsewhere_counts_on_the_swarm_client(monkeypatch):
    from scripts.routing import aggregate

    client = object()
    seen = []

    def elsewhere(redis, harness, now, own):
        seen.append((redis, harness, now, own))
        return {"alpha": 2}

    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda: client)
    monkeypatch.setattr(aggregate, "elsewhere", elsewhere)

    assert aggregate.held_elsewhere("codex", 5.0, "hive-1") == {"alpha": 2}
    assert seen == [(client, "codex", 5.0, "hive-1")]
