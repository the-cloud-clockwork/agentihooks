import pytest

from scripts.inbox import exits
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.mark.parametrize("seat", ["", "eng-1@sw"])
@pytest.mark.parametrize("state", ["delivered", "read"])
def test_stale_exit_settlement_leaves_transferred_work_with_the_successor(monkeypatch, redis, seat, state):
    inbox = InboxStore(redis)
    item = inbox.send("sender", "sw-eng-1", "contract")
    snapshot = inbox.inbox("sw-eng-1")
    inbox.seats.occupy("eng-1@sw", "sw-eng-2", 1)
    inbox.redirect(item.id, "swarm", "eng-1@sw", "handoff")
    getattr(inbox, "deliver" if state == "delivered" else "read")(item.id, "sw-eng-2")
    history = inbox.history(item.id)
    monkeypatch.setattr(inbox, "inbox", lambda address: snapshot)
    exits.settle(inbox, "sw-eng-1", seat, "exited")
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("eng-1@sw", state)
    assert inbox.history(item.id) == history


@pytest.mark.parametrize("exit_text", ["finished its task and exited", "blocked its task and exited"])
def test_late_message_keeps_the_exit_outcome_after_task_reassignment(redis, exit_text):
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    exits.settle(inbox, "sw-eng-1", "", exit_text)
    store.seats.occupy("eng-1@sw", "sw-eng-2", 2)
    item = inbox.send("sender", "sw-eng-1", "late contract")
    rows = {"t1": {"claimed_by": "sw-eng-2", "state": "claimed"}}
    exits.sweep(inbox, "sw", store, rows)
    assert inbox.get(item.id).state == "cancelled"
    assert exit_text in inbox.get(item.id).reason
    [notice] = inbox.pending_items("sender")
    assert item.id in notice.text
