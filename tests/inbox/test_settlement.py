import pytest

from scripts.inbox.store import InboxStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    import fakeredis

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
    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: int(clock[0] * 1000))
    monkeypatch.setattr(delivery, "now_ms", lambda: int(clock[0] * 1000))
    monkeypatch.setattr(delivery, "connect", lambda environ: store)
    env = {"AGENTIHOOKS_AGENT_NAME": "bob", "AGENTIHOOKS_INBOX_RETRY_WINDOW_S": "300"}
    item = store.send("alice", "bob", "Verify the deployed proof")
    store.deliver(item.id, "bob")
    store.confirm(item.id, "bob")
    second = store.send("alice", "bob", "Verify the second proof")
    store.deliver(second.id, "bob")
    store.confirm(second.id, "bob")
    history = store.history(item.id)

    clock[0] = 1299.0
    assert delivery.pending_context("receipt-session", env) == ""
    clock[0] = 1300.0
    reminder = delivery.pending_context("receipt-session", env)
    assert reminder == (
        "=== INBOX: awaiting explicit settlement ===\n"
        f"Confirmed or read work items: {item.id}, {second.id}.\n"
        "After handling each item, use agentihooks msg reply <id> <text> or "
        "agentihooks msg close <id> done <outcome> to name where the work went. "
        "Keep unresolved work and proof checks open; receipt alone does not settle work."
    )
    assert store.get(item.id).state == "confirmed"
    assert store.history(item.id) == history
    assert delivery.pending_context("receipt-session", env) == ""
    clock[0] = 1599.0
    assert delivery.pending_context("receipt-session", env) == ""
    clock[0] = 1600.001
    assert item.id in delivery.pending_context("receipt-session", env)
    store.close(item.id, "bob", "done", "deployed proof verified")
    store.close(second.id, "bob", "done", "second proof verified")
    clock[0] = 1900.0
    assert delivery.pending_context("receipt-session", env) == ""


@pytest.mark.parametrize("receipt", ["read", "confirm"])
def test_information_receipt_enforces_receiver_ownership(store, receipt):
    from scripts.inbox.store import InboxError

    item = store.send("alice", "bob", "The branch merged", fyi=True)
    store.deliver(item.id, "bob")
    with pytest.raises(InboxError, match="belongs to bob"):
        getattr(store, receipt)(item.id, "carol")
    assert store.get(item.id).state == "delivered"


def test_another_session_cannot_confirm_information(store):
    item = store.send("alice", "bob", "The branch merged", fyi=True)
    store.deliver(item.id, "bob")
    successor = InboxStore(store.redis, session_id="successor-session")
    assert successor.confirm(item.id, "bob") is None
    assert store.get(item.id).state == "delivered"


def test_hook_information_requests_no_action_and_closes_after_receipt(store, monkeypatch):
    from hooks.context import inbox_delivery as delivery

    clock = [1_000_000]
    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: clock[0])
    monkeypatch.setattr(delivery, "now_ms", lambda: clock[0])
    monkeypatch.setattr(delivery, "connect", lambda environ: store)
    env = {"AGENTIHOOKS_AGENT_NAME": "bob"}
    item = store.send("alice", "bob", "The branch merged", fyi=True)
    context = delivery.pending_context("receipt-session", env)
    assert context == (
        f"=== INBOX: message {item.id} from alice ===\nThe branch merged\n"
        "Information only; closes on read or confirmed receipt."
    )
    assert store.get(item.id).state == "delivered"
    clock[0] += 1
    assert delivery.pending_context("receipt-session", env) == ""
    assert store.get(item.id).reason == "done: information received"


def test_channel_information_requests_no_action_and_work_keeps_its_content(store):
    from scripts.inbox.channel import event

    info = store.send("alice", "bob", "The branch merged", fyi=True)
    work = store.send("alice", "bob", "Verify the proof")
    assert (
        event(info)["params"]["content"] == "Information only; closes on read or confirmed receipt.\nThe branch merged"
    )
    assert event(work)["params"]["content"] == "Verify the proof"


def test_existing_reminder_debt_is_shared_by_reconnections_and_scoped_to_the_receiver(store, monkeypatch):
    from hooks.context import inbox_delivery as delivery

    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: 1_000_000)
    monkeypatch.setattr(delivery, "now_ms", lambda: 1_300_000)
    monkeypatch.setattr(delivery, "connect", lambda environ: InboxStore(store.redis, session_id="reconnected"))
    for receiver in ["bob", "carol"]:
        item = store.send("alice", receiver, "Verify the deployed proof")
        store.deliver(item.id, receiver)
        store.confirm(item.id, receiver)
    store.redis.set(store.key("settlement-reminder", "bob"), "shown", px=300_000)
    assert delivery.pending_context("reconnected", {"AGENTIHOOKS_AGENT_NAME": "bob"}) == ""
    assert item.id in delivery.pending_context("reconnected", {"AGENTIHOOKS_AGENT_NAME": "carol"})


def test_legacy_open_work_is_recovered_and_recent_read_or_unconfirmed_work_is_not_settlement_debt(store, monkeypatch):
    from hooks.context import inbox_delivery as delivery

    clock = [1_000_000]
    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: clock[0])
    monkeypatch.setattr(delivery, "now_ms", lambda: clock[0])
    monkeypatch.setattr(delivery, "connect", lambda environ: store)
    overdue = store.send("alice", "bob", "Verify the overdue proof")
    store.deliver(overdue.id, "bob")
    store.confirm(overdue.id, "bob")
    recent = store.send("alice", "bob", "Verify the recently read proof")
    unconfirmed = store.send("alice", "bob", "New receiver work")
    information = store.send("alice", "bob", "Old information", fyi=True)
    store.redis.hset(store.key("item", information.id), mapping={"state": "confirmed"})
    store.redis.delete(store.key("open", "bob"), store.key("open-indexed", "bob"), store.key("open-size", "bob"))
    clock[0] = 1_299_000
    store.read(recent.id, "bob")
    clock[0] = 1_300_000
    context = delivery.pending_context("receipt-session", {"AGENTIHOOKS_AGENT_NAME": "bob"})
    reminder = context.split("\n\n")[-1]
    assert overdue.id in reminder
    assert recent.id not in reminder
    assert unconfirmed.id not in reminder
    assert information.id not in reminder
    assert store.get(overdue.id).state == "confirmed"
    assert store.get(recent.id).state == "read"
    assert store.get(unconfirmed.id).state == "delivered"


def test_warmed_inbox_indexes_do_not_reload_closed_messages_for_a_settlement_reminder(store, monkeypatch):
    from hooks.context import inbox_delivery as delivery

    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: 1_000_000)
    monkeypatch.setattr(delivery, "now_ms", lambda: 1_300_000)
    monkeypatch.setattr(delivery, "connect", lambda environ: store)
    for _ in range(1000):
        item = store.send("alice", "bob", "Previously handled work")
        store.close(item.id, "bob", "done", "handled the request")
    active = store.send("alice", "bob", "Verify the deployed proof")
    store.deliver(active.id, "bob")
    store.confirm(active.id, "bob")
    assert [item.id for item in store.open_items("bob")] == [active.id]
    store.pending_items("bob")
    store.redeliver(0, 0)
    loaded = []
    prefix = store.key("item") + ":"
    redis_execute = store.redis.execute_command
    pipeline_type = type(store.redis.pipeline())
    pipeline_execute = pipeline_type.execute_command

    def record(args):
        if args[0] == "HGETALL" and args[1].startswith(prefix):
            loaded.append(args[1])

    def counted_redis(*args, **kwargs):
        record(args)
        return redis_execute(*args, **kwargs)

    def counted_pipeline(pipe, *args, **kwargs):
        record(args)
        return pipeline_execute(pipe, *args, **kwargs)

    monkeypatch.setattr(store.redis, "execute_command", counted_redis)
    monkeypatch.setattr(pipeline_type, "execute_command", counted_pipeline)
    assert active.id in delivery.pending_context("receipt-session", {"AGENTIHOOKS_AGENT_NAME": "bob"})
    assert loaded == [store.key("item", active.id)]


@pytest.mark.parametrize("receipt", ["read", "confirm"])
def test_reminders_include_only_the_receivers_due_work_and_preserve_seat_history(store, monkeypatch, receipt):
    from hooks.context import inbox_delivery as delivery

    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: 1_000_000)
    monkeypatch.setattr(delivery, "now_ms", lambda: 1_100_000)
    monkeypatch.setattr(delivery, "connect", lambda environ: store)
    store.seats.occupy("master@sw", "bob", 1_000_000)
    own = store.send("alice", "master@sw", "Verify the deployed proof")
    other = store.send("alice", "carol", "Verify another proof")
    for item, receiver in [(own, "bob"), (other, "carol")]:
        store.deliver(item.id, receiver)
        getattr(store, receipt)(item.id, receiver)
    recent = store.send("alice", "bob", "Keep this pending check open")
    history = store.history(own.id)
    env = {"AGENTIHOOKS_AGENT_NAME": "bob", "AGENTIHOOKS_INBOX_RETRY_WINDOW_S": "100"}

    context = delivery.pending_context("receipt-session", env)
    assert own.id in context
    assert other.id not in context
    assert "awaiting explicit settlement" in context
    assert recent.id in context
    assert store.get(recent.id).state == "delivered"
    assert store.history(own.id) == history
    assert store.get(own.id).state == ("read" if receipt == "read" else "confirmed")
    assert store.get(other.id).state == ("read" if receipt == "read" else "confirmed")
