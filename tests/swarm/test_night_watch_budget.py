import time
from types import SimpleNamespace

import pytest

from scripts.swarm import redis_budget
from scripts.swarm.health import merge_stall, night_watch
from scripts.swarm.store import MANUAL_SCALING, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

SLUG = "sw"
STALL_S = 0.05


class Stalled:
    def __init__(self, client):
        self.client, self.calls = client, 0

    def __getattr__(self, name):
        attr = getattr(self.client, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            self.calls += 1
            time.sleep(STALL_S)
            return attr(*args, **kwargs)

        return call


def stalled_store():
    import fakeredis

    fake = fakeredis.FakeRedis(decode_responses=True)
    RedisStore(fake).create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=0, max_plan=0, scaling=MANUAL_SCALING))
    RedisStore(fake).update(SLUG, state="running")
    return RedisStore(Stalled(fake))


def watch(store):
    runtime = SimpleNamespace(live_names=lambda: set())
    return night_watch.Watch(store, SLUG, SimpleNamespace(), runtime, {"tasks": []}, 1_000, False, lambda: [])


@pytest.mark.wall_clock
def test_a_stalled_redis_ends_the_pass_within_its_budget_and_records_a_skip(monkeypatch):
    monkeypatch.setattr(night_watch, "BUDGET_S", 0.2)
    monkeypatch.setattr(merge_stall, "findings", lambda *args: [])
    store = stalled_store()

    started = time.monotonic()
    actions = night_watch.run(watch(store))

    assert time.monotonic() - started < 0.2 + 2 * STALL_S + 1
    assert store.redis.calls <= 0.2 / STALL_S + 2
    assert actions == ["skipped the rest of the night watch pass: its 0.2 second Redis budget is spent"]
    assert night_watch.read(RedisStore(store.redis.client), SLUG)["at"] == 1_000


def test_a_pass_inside_its_budget_saves_its_state_and_records_no_skip(monkeypatch):
    monkeypatch.setattr(merge_stall, "findings", lambda *args: [])
    store = stalled_store()

    assert night_watch.run(watch(store)) == []
    assert night_watch.read(store, SLUG) == {"at": 1_000, night_watch.SEAT: {"since": 1_000}}


def test_a_redis_failure_records_a_skip_instead_of_failing_the_tick(capsys):
    from redis.exceptions import TimeoutError as RedisTimeout

    def timed_out(*args):
        raise RedisTimeout("Timeout reading from socket")

    store = SimpleNamespace(redis=SimpleNamespace(get=timed_out), key=lambda *parts: ":".join(parts))

    assert night_watch.run(watch(store)) == [
        "skipped the rest of the night watch pass: Redis failed: Timeout reading from socket"
    ]
    assert capsys.readouterr().err == (
        "night watch skipped the rest of its pass, Redis failed: Timeout reading from socket\n"
    )


class SetRefused(Stalled):
    def __getattr__(self, name):
        if name != "set":
            return getattr(self.client, name)

        def refused(*args, **kwargs):
            from redis.exceptions import ConnectionError as RedisConnectionError

            raise RedisConnectionError("Connection reset by peer")

        return refused


def test_a_failed_state_save_records_a_skip(monkeypatch, capsys):
    import fakeredis

    monkeypatch.setattr(merge_stall, "findings", lambda *args: [])
    fake = fakeredis.FakeRedis(decode_responses=True)
    RedisStore(fake).create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=0, max_plan=0, scaling=MANUAL_SCALING))
    RedisStore(fake).update(SLUG, state="running")

    assert night_watch.run(watch(RedisStore(SetRefused(fake)))) == [
        "skipped the rest of the night watch pass: Redis failed: Connection reset by peer"
    ]
    assert (
        capsys.readouterr().err == "night watch skipped the rest of its pass, Redis failed: Connection reset by peer\n"
    )


def test_a_spent_budget_refuses_the_next_call_without_reaching_redis():
    client = SimpleNamespace(get=lambda key: pytest.fail("a spent budget reached Redis"), name="client")
    budget = redis_budget.Budget(client, 0)

    assert budget.name == "client"
    with pytest.raises(redis_budget.Spent, match="^its 0 second Redis budget is spent$"):
        budget.get("key")
    assert budget.why == "its 0 second Redis budget is spent"


def test_a_budget_passes_calls_through_while_time_remains():
    client = SimpleNamespace(get=lambda key, default=None: (key, default))

    assert redis_budget.Budget(client, 60).get("key", default=1) == ("key", 1)
    assert redis_budget.Budget(client, 60).why == ""


def test_a_real_client_gets_its_own_pool_with_a_bounded_timeout_and_no_retries():
    import redis

    client = redis.Redis.from_url("redis://127.0.0.1:1/3", decode_responses=True, socket_timeout=10)
    capped = redis_budget.capped(client)
    options = capped.connection_pool.connection_kwargs

    assert capped.connection_pool is not client.connection_pool
    assert capped.connection_pool.connection_class is client.connection_pool.connection_class
    assert options["socket_timeout"] == redis_budget.CALL_TIMEOUT_S == 5
    assert options["retry"].get_retries() == 0
    assert (options["db"], options["decode_responses"]) == (3, True)
    assert client.connection_pool.connection_kwargs["socket_timeout"] == 10


def test_the_capped_pool_keeps_the_connection_class_and_never_backs_off():
    import redis
    from redis.backoff import NoBackoff

    client = redis.Redis.from_url("unix:///run/redis-budget-test.sock")
    pool = redis_budget.capped(client).connection_pool

    assert pool.connection_class is redis.UnixDomainSocketConnection
    assert type(pool.connection_kwargs["retry"]._backoff) is NoBackoff


def test_a_budget_refuses_at_its_exact_deadline(monkeypatch):
    monkeypatch.setattr(redis_budget.time, "monotonic", lambda: 100.0)
    budget = redis_budget.Budget(SimpleNamespace(get=lambda key: pytest.fail("reached Redis at the deadline")), 0)

    with pytest.raises(redis_budget.Spent):
        budget.get("key")


def test_a_client_that_is_not_a_plain_redis_stays_as_it_is():
    import fakeredis

    client = fakeredis.FakeRedis()

    assert redis_budget.capped(client) is client


def test_release_disconnects_only_a_pool_built_for_the_pass():
    disconnected = []
    capped = SimpleNamespace(connection_pool=SimpleNamespace(disconnect=lambda: disconnected.append(True)))
    source = SimpleNamespace(connection_pool=SimpleNamespace(disconnect=lambda: pytest.fail("source disconnected")))

    redis_budget.release(source, source)
    redis_budget.release(capped, source)

    assert disconnected == [True]


def test_bounded_routes_the_store_and_every_part_holding_its_client_through_the_budget():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    budget = redis_budget.Budget(store.redis, 60)
    bound = redis_budget.bounded(store, budget)

    assert type(bound) is RedisStore
    assert bound.redis is budget
    assert (bound.seats.redis, bound.memory.redis, bound.culture.redis, bound.names.redis) == (budget,) * 4
    assert bound.execution_registry.redis is budget
    assert bound.execution_registry.store is bound
    assert store.execution_registry.store is store
    assert store.redis is not budget and store.seats.redis is store.redis
    assert bound.names.mint is store.names.mint
