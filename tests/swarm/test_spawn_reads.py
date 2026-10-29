import pytest

from scripts.swarm import tick as tick_module
from tests.swarm import test_tick

pytestmark = pytest.mark.unit
store = test_tick.store
FakeLedger = test_tick.FakeLedger


def refuse(*args):
    raise AssertionError(f"read one task at a time: {args}")


def ids(found):
    return [task["id"] for task in found]


def test_a_claim_view_reads_every_open_claim_in_one_call(store, monkeypatch):
    rows = FakeLedger(
        [
            {"id": "t1", "lane": "eng"},
            {"id": "t2", "lane": "eng"},
            {"id": "t3", "lane": "eng", "state": "done"},
        ]
    )
    store.claim("sw", "t2", "eng-other", tick_module.LEASE_MS)
    store.claim("sw", "t3", "eng-done", tick_module.LEASE_MS)
    monkeypatch.setattr(store, "claimant", refuse)
    reads = []
    mget = store.redis.mget
    monkeypatch.setattr(store.redis, "mget", lambda keys: reads.append(list(keys)) or mget(keys))
    doc = rows.state("sw")
    claims = tick_module.claim_view("sw", store, rows.rows, doc)
    assert reads == [[store.key("sw", "claim", "t1"), store.key("sw", "claim", "t2")]]
    assert [claims.view.claimed(task_id) for task_id in ("t1", "t2", "t3")] == [False, True, False]
    assert ids(tick_module.ready_in_order("sw", store, rows.rows, doc, "eng", claims=claims)) == ["t1"]


def test_claimed_answers_an_empty_task_list_without_reading(store, monkeypatch):
    monkeypatch.setattr(store.redis, "mget", refuse)
    assert store.claimed("sw", []) == set()


def test_launch_order_reads_every_launch_failure_in_one_call(store, monkeypatch):
    store.note_launch_failure("sw", "t1", "init-agent exit 1")
    monkeypatch.setattr(store, "launch_failure", refuse)
    tasks = [{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "eng"}]
    assert ids(tick_module._launch_order("sw", store, tasks)) == ["t2", "t1"]
    assert store.launch_failures("sw") == {"t1": "init-agent exit 1"}
