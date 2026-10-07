from argparse import Namespace
from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm.control_notifications import notify
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def setup():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("proof", "/repo", max_eng=0, max_ci=0))
    name = store.names.next("proof", "master", at=1)
    master = AgentRecord(name, "master", "", seat="master@proof")
    store.put_agent("proof", master)
    store.seats.occupy(master.seat, name, 1)
    return store, InboxStore(store.redis), master


@pytest.fixture
def close(setup, monkeypatch):
    from scripts.swarm import cli
    from tests.swarm.test_tick import FakeRuntime

    store, _, _ = setup
    ledger = SimpleNamespace(summarize=lambda *args: None, mark_closed=lambda *args: None, tasks=lambda *args: [])
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "HerdrRuntime", FakeRuntime)
    monkeypatch.setattr(cli.snapshot, "take", lambda *args: "snapshot")
    return lambda: cli.cmd_close(store, Namespace(slug="proof", name="operator", now=True, note="Proof ended"))


@pytest.mark.parametrize("action", ["stop", "close"])
def test_terminal_notice_has_no_retired_master_recipient(setup, action):
    store, inbox, master = setup
    old = inbox.send("operator", master.seat, "Resume this work")
    inbox.deliver(old.id, master.name)
    history = inbox.history(old.id)
    store.drop_agent("proof", master.name)
    store.update("proof", state="stopped")
    said = []
    ledger = SimpleNamespace(say=lambda *args, **kwargs: said.append(args))
    notify(store, Namespace(slug="proof", name="operator", now=True), ledger, master, action)
    assert len(said) == 1
    assert [item.id for item in inbox.inbox(master.seat)] == [old.id]
    assert inbox.history(old.id) == history


@pytest.mark.parametrize("state", ["pending", "delivered", "read"])
def test_closing_a_swarm_settles_its_seat_mail(setup, close, state):
    _, inbox, master = setup
    item = inbox.send("operator", master.seat, "Outstanding proof", fyi=True)
    if state != "pending":
        getattr(inbox, "deliver" if state == "delivered" else "read")(item.id, master.name)
    close()
    closed = inbox.get(item.id)
    assert closed.state == "cancelled"
    assert "swarm closed" in closed.reason
    assert inbox.history(item.id)[-1]["state"] == "cancelled"


def test_close_settles_mail_even_when_the_master_retired_earlier(setup, close):
    store, inbox, master = setup
    item = inbox.send("operator", master.seat, "Proof completed")
    inbox.deliver(item.id, master.name)
    store.drop_agent("proof", master.name)
    other = inbox.send(master.name, "master@other", "Other swarm work")
    old_history = inbox.history(other.id)
    close()
    assert inbox.get(item.id).state == "cancelled"
    assert "swarm closed" in inbox.get(item.id).reason
    assert inbox.history(other.id) == old_history


@pytest.mark.parametrize("address", ["proof-master-1", "ce5108a4-c06b-43f1-be80-9ba9491356e5"])
def test_wake_settles_an_unresolved_recipient(setup, monkeypatch, address):
    from scripts.inbox import wake
    from tests.inbox.test_wake import FakeHerdr, FakeLedger

    store, inbox, master = setup
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {})
    item = inbox.send("departed", address, "Gone receiver")
    ledger = FakeLedger()
    wake.wake_pass(inbox, "proof", store.agents("proof"), FakeHerdr({}), ledger, item.created_at + 300_000, 300_000)
    assert inbox.get(item.id).state == "cancelled"
    assert "recipient no longer resolves" in inbox.get(item.id).reason
    assert ledger.followups == []


@pytest.mark.parametrize("state", ["pending", "delivered", "read"])
def test_a_resumable_master_keeps_mail_after_retirement(setup, monkeypatch, state):
    from scripts.inbox import exits, wake
    from tests.inbox.test_wake import FakeHerdr, FakeLedger

    store, inbox, master = setup
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {})
    item = inbox.send("operator", master.seat, "Resume work")
    if state != "pending":
        getattr(inbox, "deliver" if state == "delivered" else "read")(item.id, master.name)
    history = inbox.history(item.id)
    store.drop_agent("proof", master.name)
    exits.settle(inbox, master.name, master.seat, "handed off its seat")
    wake.wake_pass(inbox, "proof", [], FakeHerdr({}), FakeLedger(), item.created_at + 300_000, 300_000)
    assert inbox.get(item.id).state == state
    assert inbox.history(item.id)[: len(history)] == history
    successor = store.names.next("proof", "master", at=2)
    inbox.seats.occupy(master.seat, successor, 2)
    assert [mail.id for mail in inbox.mailbox(successor)] == [item.id]
