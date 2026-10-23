import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime


@pytest.fixture
def held():
    import fakeredis

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


def test_clearing_a_scheduled_window_starts_a_new_hour_without_a_window(held):
    store, ledger, runtime = held
    tick("sw", store, ledger, runtime, 2)
    ledger.rows["t1"]["contract"]["after_at"] = 20_000_000
    tick("sw", store, ledger, runtime, 4_000_000)
    ledger.rows["t1"]["contract"]["after_at"] = ""
    ledger.stamps["tasks/t1/contract"] = {"at": 5_000_000}
    tick("sw", store, ledger, runtime, 5_000_001)
    assert notices(store) == []
    tick("sw", store, ledger, runtime, 8_600_000)
    assert notices(store) == []
    tick("sw", store, ledger, runtime, 8_600_001)
    assert len(notices(store)) == 1


@pytest.mark.parametrize(
    "fields", [{"state": "done"}, {"state": "blocked"}, {"awaiting": "fix"}, {"out_of_scope": True}]
)
def test_only_open_verification_holds_need_a_master_notice(held, fields):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    ledger.rows["t1"].update(fields)
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 3_600_002)
    assert notices(store) == []


def test_an_unstamped_hold_is_dated_from_first_observation(held):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    ledger.stamps = {}
    for at in (1, 3_600_001):
        claim_hold.verification_notices("sw", store, ledger.state("sw"), at)
        assert notices(store) == []
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 3_600_002)
    assert len(notices(store)) == 1
