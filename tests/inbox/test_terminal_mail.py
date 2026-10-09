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

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
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


@pytest.mark.parametrize("params", [{}, {"host": "localhost", "port": 6379}])
def test_setup_reads_no_key_another_fake_client_writes(setup, params):
    import fakeredis

    store, _, _ = setup
    other = fakeredis.FakeRedis(decode_responses=True, **params)
    other.set("stray", "1")
    try:
        assert store.redis.get("stray") is None
    finally:
        other.delete("stray")


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
    assert inbox.history(item.id)[-1]["by"] == "swarm"


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


def test_a_later_tick_settles_a_closed_swarms_mail_after_retirement_finishes(setup):
    from scripts.swarm.tick import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store, inbox, master = setup
    item = inbox.send("operator", master.seat, "Close proof")
    inbox.deliver(item.id, master.name)
    store.drop_agent("proof", master.name)
    store.update("proof", state="stopped")
    ledger = FakeLedger([])
    original_state = ledger.state
    ledger.state = lambda slug: {**original_state(slug), "closed_at": 1}
    ledger.bin_closed = lambda *args: False
    tick("proof", store, ledger, FakeRuntime(), item.created_at + 1)
    assert inbox.get(item.id).state == "cancelled"
    assert "swarm closed" in inbox.get(item.id).reason


@pytest.mark.parametrize("address", ["proof-master-1", "ce5108a4-c06b-43f1-be80-9ba9491356e5", "operator-desk"])
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
    assert inbox.history(item.id)[-1]["by"] == "swarm"
    assert ledger.followups == []


@pytest.mark.parametrize("state", ["pending", "delivered", "read"])
def test_a_resumable_master_keeps_mail_after_retirement(setup, monkeypatch, state):
    from scripts.inbox import exits, wake
    from tests.inbox.test_wake import FakeHerdr, FakeLedger

    store, inbox, master = setup
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {})
    clock = [1_000_000]
    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: clock[0])
    item = inbox.send("operator", master.seat, "Resume work")
    clock[0] += 1
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


@pytest.mark.parametrize(
    ("kind", "keep"),
    [
        ("operator", True),
        ("session_name", True),
        ("session_id", True),
        ("registered", True),
        ("retired", False),
        ("seat_without_exit", True),
        ("handoff", True),
        ("ended", False),
        ("missing_name", False),
    ],
)
def test_wake_preserves_only_live_or_resumable_personal_recipients(setup, monkeypatch, kind, keep):
    from scripts.inbox import wake
    from tests.inbox.test_wake import FakeHerdr, FakeLedger

    store, inbox, _ = setup
    sessions = {}
    if kind == "operator":
        address = "operator"
    elif kind in {"session_name", "session_id", "missing_name"}:
        sessions = {"session-id": {"name": "session-name"}} if kind != "missing_name" else {"session-id": {}}
        address = {"session_name": "session-name", "session_id": "session-id", "missing_name": "XXXX"}[kind]
    else:
        address = store.names.next("proof", "eng", at=2)
        if kind != "registered":
            store.names.retire(address, 3)
        if kind in {"seat_without_exit", "handoff", "ended"}:
            inbox.seats.occupy("eng-1@proof", address, 2)
        if kind in {"handoff", "ended"}:
            inbox.seats.record_exit(address, "eng-1@proof" if kind == "handoff" else "", kind)
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: sessions)
    item = inbox.send("departed", address, "Recipient control", fyi=True)
    actions = wake.wake_pass(inbox, "proof", [], FakeHerdr({}), FakeLedger(), item.created_at + 300_000, 300_000)
    assert inbox.get(item.id).state == ("pending" if keep else "cancelled")
    assert actions == ([] if keep else [f"closed message {item.id}: recipient no longer resolves"])


def test_wake_does_not_cancel_a_new_message_before_the_retry_window(setup, monkeypatch):
    from scripts.inbox import wake
    from tests.inbox.test_wake import FakeHerdr, FakeLedger

    _, inbox, _ = setup
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {})
    item = inbox.send("departed", "gone-name", "New mail")
    wake.wake_pass(inbox, "proof", [], FakeHerdr({}), FakeLedger(), item.created_at + 299_999, 300_000)
    assert inbox.get(item.id).state == "pending"


def test_wake_rejects_a_session_whose_process_died(setup, monkeypatch):
    from hooks.context import broadcast
    from scripts.inbox import wake
    from tests.inbox.test_wake import FakeHerdr, FakeLedger

    _, inbox, _ = setup
    monkeypatch.setattr(
        broadcast, "_load_sessions", lambda: {"dead-session": {"name": "dead-name", "pid": 123, "status": "alive"}}
    )
    monkeypatch.setattr(broadcast, "_save_sessions", lambda rows: None)

    def dead_process(pid, signal):
        raise ProcessLookupError

    monkeypatch.setattr(broadcast.os, "kill", dead_process)
    item = inbox.send("departed", "dead-name", "Process is gone")
    wake.wake_pass(inbox, "proof", [], FakeHerdr({}), FakeLedger(), item.created_at + 300_000, 300_000)
    assert inbox.get(item.id).state == "cancelled"


def test_control_notices_have_unique_nonempty_references(setup):
    store, inbox, master = setup
    ledger = SimpleNamespace(say=lambda *args, **kwargs: None)
    args = Namespace(slug="proof", name="operator", now=True)
    notify(store, args, ledger, master, "pause")
    notify(store, args, ledger, master, "pause")
    items = inbox.inbox(master.seat)
    assert len(items) == 2
    assert all(item.ref for item in items)
    assert items[0].ref != items[1].ref


@pytest.mark.parametrize("closing", [False, True])
def test_settlement_preserves_mail_transferred_during_the_close(setup, close, monkeypatch, closing):
    from scripts.inbox import wake
    from tests.inbox.test_wake import FakeHerdr, FakeLedger

    store, inbox, master = setup
    address = master.seat if closing else "gone-name"
    item = inbox.send("operator", address, "Transfer before settlement")
    withdraw = InboxStore.withdraw
    moved = False

    def handover(current, ident, by, reason, expected_address=""):
        nonlocal moved
        if ident == item.id and not moved:
            moved = True
            current.redirect(ident, "operator", "master@other", "Transferred to the other swarm", address)
        return withdraw(current, ident, by, reason, expected_address)

    monkeypatch.setattr(InboxStore, "withdraw", handover)
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {})
    if closing:
        close()
    else:
        actions = wake.wake_pass(
            inbox, "proof", store.agents("proof"), FakeHerdr({}), FakeLedger(), item.created_at + 300_000, 300_000
        )
        assert actions == []
    assert inbox.get(item.id).address == "master@other"
    assert inbox.get(item.id).state == "pending"
    assert inbox.get(item.id).reason == "Transferred to the other swarm"


def test_close_settles_registered_personal_mail_and_keeps_closed_history(setup, close):
    store, inbox, master = setup
    personal = inbox.send("operator", master.name, "Personal proof result")
    already = inbox.send("operator", master.seat, "Finished proof")
    inbox.close(already.id, master.name, "done", "Proof finished")
    history = inbox.history(already.id)
    close()
    assert inbox.get(personal.id).state == "cancelled"
    assert inbox.history(personal.id)[-1]["by"] == "swarm"
    assert inbox.history(already.id) == history


def test_close_ignores_other_redis_keys_while_mail_is_transferred(setup, close, monkeypatch):
    import fnmatch

    _, inbox, master = setup
    item = inbox.send("operator", master.seat, "Transfer during the scan")
    prefix = inbox.key("address", "")
    noise = "x" * len(prefix) + master.seat
    mailbox = prefix + master.seat
    scan = inbox.redis.scan_iter

    def scan_with_transfer(match=None, **kwargs):
        if match and "swarm:seat" in match:
            yield from scan(match=match, **kwargs)
            return
        for key in (noise, mailbox):
            if match is None or fnmatch.fnmatch(key, match):
                yield key
            if key == noise:
                inbox.redirect(item.id, "operator", "master@other", "Transferred before settlement", master.seat)

    monkeypatch.setattr(inbox.redis, "scan_iter", scan_with_transfer)
    close()
    assert inbox.get(item.id).state == "pending"
    assert inbox.get(item.id).address == "master@other"
    assert inbox.get(item.id).reason == "Transferred before settlement"
