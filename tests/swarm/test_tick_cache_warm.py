import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import idle, stale_nudge
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.helpers.seat import seat_owner
from tests.swarm.test_tick import IDLER, STALE_MS, FakeRuntime, stale_nudges, tasks, tick


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return s


def cache_items(store):
    return [item for item in InboxStore(store.redis).inbox(IDLER) if item.ref.startswith(f"{stale_nudge.CACHE_REF}:")]


@pytest.mark.feature("stale-nudge-cache-warm")
def test_the_tick_sends_a_quiet_agent_in_a_named_wait_one_cache_warm_item_past_forty_minutes(store: RedisStore) -> None:
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seat_owner(store, ledger, runtime, "t1", 1_000)
    store.update("sw", state="paused")
    runtime.statuses[IDLER] = "idle"
    idle.declare_wait(store.redis, "sw", IDLER, 1_000 + 9 * STALE_MS, "checks on the pull request", 1_000)
    tick("sw", store, ledger, runtime, now_ms=1_000 + 4 * STALE_MS)
    assert cache_items(store) == []
    actions = tick("sw", store, ledger, runtime, now_ms=1_001 + 4 * STALE_MS)
    [item] = cache_items(store)
    assert f"sent {IDLER} a cache warm check through its inbox as message {item.id}" in actions
    assert (item.sender, item.task) == ("swarm", "t1") and stale_nudges(store) == [] and runtime.nudged == []
