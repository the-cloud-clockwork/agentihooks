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
    actions = tick("sw", store, ledger, runtime, 3_600_002)
    assert "told the master about verification hold t1" in actions
    tick("sw", store, ledger, runtime, 3_600_003)
    items = notices(store)
    assert len(items) == 1
    assert items[0].sender == "swarm"
    assert items[0].text == (
        "Task t1 (Hive telemetry) awaits verification without a proof window "
        "for more than an hour. Set contract.after_at to its proof time, or reopen it with "
        "agentihooks ledger --slug sw --as master task set t1 awaiting="
    )
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


@pytest.mark.parametrize("metadata", [{}, {"_meta": {}}])
def test_a_document_without_stamps_times_the_hold_from_observation(held, metadata):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    doc = {"tasks": ledger.tasks("sw"), **metadata}
    for at in (100, 3_600_100):
        assert claim_hold.verification_notices("sw", store, doc, at) == []
        assert notices(store) == []
    assert claim_hold.verification_notices("sw", store, doc, 3_600_101) == [
        "told the master about verification hold t1"
    ]
    assert len(notices(store)) == 1


@pytest.mark.parametrize(
    "earlier",
    [
        {"state": "done"},
        {"contract": {"after_at": 20_000_000}},
        {"recent": True},
        {"notified": True},
    ],
)
def test_a_skipped_hold_does_not_hide_the_eligible_task_behind_it(held, earlier):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    doc = ledger.state("sw")
    other = {**doc["tasks"][0], "id": "t0", **earlier}
    doc["tasks"].insert(0, other)
    ledger.stamps["tasks/t0/awaiting"] = {"at": 9_999_900 if earlier.get("recent") else 1}
    if earlier.get("notified"):
        claim_hold.verification_notices("sw", store, {**doc, "tasks": [other]}, 9_999_999)
    assert claim_hold.verification_notices("sw", store, doc, 10_000_000) == [
        "told the master about verification hold t1"
    ]
    assert len(notices(store)) == (2 if earlier.get("notified") else 1)


def test_each_task_keeps_its_own_observed_hold_and_daily_notice(held):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    ledger.stamps = {}
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 100)
    ledger.rows["t2"] = {**ledger.rows["t1"], "id": "t2"}
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 200)
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 3_600_101)
    assert [item.text.split()[1] for item in notices(store)] == ["t1"]
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 3_600_201)
    assert {item.text.split()[1] for item in notices(store)} == {"t1", "t2"}


def test_the_same_task_in_two_swarms_has_independent_hold_and_notice_times(held):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    ledger.stamps = {}
    doc = ledger.state("sw")
    claim_hold.verification_notices("sw", store, doc, 100)
    claim_hold.verification_notices("other", store, doc, 200)
    claim_hold.verification_notices("sw", store, doc, 3_600_101)
    claim_hold.verification_notices("other", store, doc, 3_600_101)
    assert InboxStore(store.redis).inbox("master@other") == []
    claim_hold.verification_notices("other", store, doc, 3_600_201)
    items = InboxStore(store.redis).inbox("master@other")
    assert len(items) == 1
    assert "task set t1 awaiting=" in items[0].text
