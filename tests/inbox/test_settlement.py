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


def test_confirmed_work_reminds_its_receiver_until_explicit_settlement(store, monkeypatch):
    from hooks.context import inbox_delivery as delivery

    clock = [1000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    monkeypatch.setattr(delivery, "connect", lambda environ: store)
    env = {"AGENTIHOOKS_AGENT_NAME": "bob", "AGENTIHOOKS_INBOX_RETRY_WINDOW_S": "300"}
    item = store.send("alice", "bob", "Verify the deployed proof")
    store.deliver(item.id, "bob")
    store.confirm(item.id, "bob")
    history = store.history(item.id)

    clock[0] = 1299.0
    assert delivery.pending_context("receipt-session", env) == ""
    clock[0] = 1300.0
    reminder = delivery.pending_context("receipt-session", env)
    assert item.id in reminder
    assert "settlement" in reminder
    assert "agentihooks msg reply" in reminder
    assert "Keep unresolved work and proof checks open" in reminder
    assert store.get(item.id).state == "confirmed"
    assert store.history(item.id) == history
    assert delivery.pending_context("receipt-session", env) == ""
    clock[0] = 1599.0
    assert delivery.pending_context("receipt-session", env) == ""
    clock[0] = 1600.0
    assert item.id in delivery.pending_context("receipt-session", env)
    store.close(item.id, "bob", "done", "deployed proof verified")
    clock[0] = 1900.0
    assert delivery.pending_context("receipt-session", env) == ""
