import fakeredis
import pytest

from scripts.inbox.store import InboxStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    return InboxStore(fakeredis.FakeRedis(decode_responses=True), session_id="receipt-session")


@pytest.mark.parametrize("receipt", ["read", "confirm"])
def test_information_closes_on_receipt_and_work_needs_explicit_settlement(store, receipt):
    information = store.send("alice", "bob", "The branch merged", fyi=True)
    work = store.send("alice", "bob", "Verify the deployed proof")
    store.deliver(information.id, "bob")
    store.deliver(work.id, "bob")

    received = getattr(store, receipt)(information.id, "bob")
    assert received.state == "done"
    assert received.reason == "done: information received"
    assert store.history(information.id)[-1]["by"] == "bob"
    assert getattr(store, receipt)(work.id, "bob").state == ("read" if receipt == "read" else "confirmed")
    assert store.get(work.id).reason == ""
    assert store.close(work.id, "bob", "done", "deployed proof verified").reason == "done: deployed proof verified"


def test_pending_information_is_not_closed_by_confirmation(store):
    item = store.send("alice", "bob", "The branch merged", fyi=True)
    assert store.confirm(item.id, "bob") is None
    assert store.get(item.id).state == "pending"
