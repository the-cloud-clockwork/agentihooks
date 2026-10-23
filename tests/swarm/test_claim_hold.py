import fakeredis
import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime


@pytest.fixture
def held():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0, state="paused"))
    ledger = FakeLedger([{"id": "t1", "title": "Hive telemetry", "awaiting": "verify", "contract": {"after_at": ""}}])
    ledger.stamps = {"tasks/t1/awaiting": {"at": 1}}
    return store, ledger, FakeRuntime()


def notices(store):
    return [item for item in InboxStore(store.redis).inbox("master@sw") if "Hive telemetry" in item.text]


def test_tick_tells_the_master_once_when_a_verification_hold_has_no_window_for_over_an_hour(held):
    store, ledger, runtime = held
    tick("sw", store, ledger, runtime, 3_600_001)
    assert notices(store) == []
    tick("sw", store, ledger, runtime, 3_600_002)
    tick("sw", store, ledger, runtime, 3_600_003)
    items = notices(store)
    assert len(items) == 1
    assert "t1" in items[0].text
    assert "awaiting" in items[0].text and "reopen" in items[0].text
    tick("sw", store, ledger, runtime, 90_000_001)
    assert len(notices(store)) == 1
    tick("sw", store, ledger, runtime, 90_000_002)
    assert len(notices(store)) == 2


def test_tick_does_not_raise_a_verification_hold_with_a_future_window(held):
    store, ledger, runtime = held
    ledger.rows["t1"]["contract"]["after_at"] = 9_000_000
    tick("sw", store, ledger, runtime, 3_600_002)
    assert notices(store) == []
