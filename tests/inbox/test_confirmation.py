import pytest

import hooks.context.inbox_delivery as delivery
from hooks import hook_manager
from hooks.targets.emitter import flush
from scripts.inbox import channel, seen, wake
from scripts.inbox.store import NOTIFY, InboxError, InboxStore, redelivery_ms
from scripts.swarm.store import MASTER, AgentRecord

pytestmark = pytest.mark.xdist_group("fakeredis")

W = 300_000
ENV = "AGENTIHOOKS_INBOX_REDELIVER_S"
REDELIVERED = "redelivered: never confirmed inside the redelivery window"


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    store = InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    monkeypatch.setattr(delivery, "connect", lambda environ=None: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    monkeypatch.delenv(ENV, raising=False)
    return store


def states(store, item_id):
    return [(e["state"], e["by"], e["reason"]) for e in store.history(item_id) if "state" in e]


def delivered(store, text="review my branch"):
    item = store.send("alice", "bob", text)
    return store.deliver(item.id, "bob")


def _pre(capsys):
    hook_manager.on_pre_tool_use({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/"})
    flush("PreToolUse")
    return capsys.readouterr().out


def test_redelivery_window_defaults_to_five_minutes_and_reads_the_environment():
    assert redelivery_ms({}) == 300_000
    assert redelivery_ms({ENV: "7"}) == 7000


def test_confirm_moves_a_delivered_or_read_item_to_confirmed(store):
    item = delivered(store)
    assert store.confirm(item.id, "bob").state == "confirmed"
    assert states(store, item.id)[-1] == ("confirmed", "bob", "")
    read = store.send("alice", "bob", "second")
    store.read(read.id, "bob")
    assert store.confirm(read.id, "bob").state == "confirmed"


def test_confirm_leaves_a_pending_item_alone(store):
    item = store.send("alice", "bob", "hi")
    assert store.confirm(item.id, "bob") is None
    assert store.get(item.id).state == "pending"


def test_a_delivered_item_never_confirmed_returns_to_pending_after_the_window(store):
    item = delivered(store)
    assert store.redeliver(item.updated_at + W - 1, W) == []
    [moved] = store.redeliver(item.updated_at + W, W)
    assert (moved.id, moved.state, moved.reason) == (item.id, "pending", REDELIVERED)
    assert states(store, item.id)[-1] == ("pending", "inbox", REDELIVERED)
    assert [i.id for i in store.pending_mail("bob")] == [item.id]
    assert item.id in {i.id for i in store.pending()}


def test_a_confirmed_item_is_never_redelivered(store):
    item = delivered(store)
    store.confirm(item.id, "bob")
    assert store.redeliver(item.updated_at + 10 * W, W) == []
    assert store.get(item.id).state == "confirmed"


@pytest.mark.parametrize("finish", ["read", "close"])
def test_read_and_close_count_as_confirmation(store, finish):
    item = delivered(store)
    if finish == "read":
        store.read(item.id, "bob")
    else:
        store.close(item.id, "bob", "done", "did it")
    assert store.redeliver(item.updated_at + 10 * W, W) == []


def test_an_unconfirmed_item_is_redelivered_once_per_window(store):
    item = delivered(store)
    store.redeliver(item.updated_at + W, W)
    again = store.deliver(item.id, "bob")
    assert store.redeliver(again.updated_at + W - 1, W) == []
    assert [i.id for i in store.redeliver(again.updated_at + W, W)] == [item.id]
    assert [s for s, _, _ in states(store, item.id)] == ["pending", "delivered", "pending", "delivered", "pending"]


def test_redelivery_skips_an_item_delivered_again_after_the_cutoff(store):
    item = delivered(store)
    assert store.requeue(item.id, "inbox", REDELIVERED, before=item.updated_at - 1) is None
    assert store.get(item.id).state == "delivered"


def test_the_first_redelivery_indexes_items_delivered_inside_the_window_before_the_index_existed(store):
    recent, old = delivered(store), delivered(store, "older")
    store.redis.hset(store.key("item", old.id), "updated_at", recent.updated_at - W)
    store.redis.delete(store.key("delivered"), store.key("delivered", "built"))
    assert store.redeliver(recent.updated_at, W) == []
    assert store.redis.get(store.key("delivered", "built")) == "1"
    assert [i.id for i in store.redeliver(recent.updated_at + W, W)] == [recent.id]
    assert (store.get(recent.id).state, store.get(old.id).state) == ("pending", "delivered")


def test_redelivery_drops_index_entries_of_items_gone_or_moved_on(store):
    item = delivered(store)
    store.redis.delete(store.key("item", item.id))
    assert store.redeliver(item.updated_at + W, W) == []
    assert store.redis.zcard(store.key("delivered")) == 0


def test_requeue_publishes_so_a_waiting_transport_claims_at_once(store):
    item = delivered(store)
    sub = store.redis.pubsub()
    sub.subscribe(NOTIFY)
    assert sub.get_message(timeout=1)["type"] == "subscribe"
    store.requeue(item.id, "bob", "lost")
    assert sub.get_message(timeout=1)["data"] == "bob"
    sub.close()


def test_confirm_shown_confirms_only_this_receivers_items_delivered_before_the_event(store, monkeypatch):
    clock = iter(range(1000, 2000))
    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: next(clock))
    mine = delivered(store)
    later = delivered(store, "later")
    other = store.send("alice", "carol", "not yours")
    store.deliver(other.id, "carol")
    assert [i.id for i in store.confirm_shown("bob", later.updated_at)] == [mine.id]
    assert store.get(later.id).state == "delivered"
    store.confirm_shown("bob", later.updated_at + 1)
    assert store.get(later.id).state == "confirmed"
    assert store.get(other.id).state == "delivered"


def test_a_successor_at_a_seat_does_not_confirm_what_its_predecessor_was_shown(store):
    seat = "eng-1@sw"
    store.seats.occupy(seat, "bob", at=1)
    item = store.deliver(store.send("alice", seat, "for the seat").id, "bob")
    store.seats.occupy(seat, "carol", at=2)
    assert store.confirm_shown("carol", item.updated_at + W) == []
    assert store.get(item.id).state == "delivered"
    store.seats.occupy(seat, "bob", at=3)
    assert [i.id for i in store.confirm_shown("bob", item.updated_at + 1)] == [item.id]


def test_the_next_hook_event_confirms_an_item_shown_by_the_last_one(store, capsys, monkeypatch):
    item = store.send("alice", "bob", "review my branch")
    assert "review my branch" in _pre(capsys)
    assert store.get(item.id).state == "delivered"
    monkeypatch.setattr(delivery, "now_ms", lambda: store.get(item.id).updated_at + 1)
    _pre(capsys)
    assert store.get(item.id).state == "confirmed"
    assert store.redeliver(store.get(item.id).updated_at + 10 * W, W) == []
    assert "review my branch" not in _pre(capsys)


@pytest.mark.parametrize("event", ["stop", "prompt"])
def test_stop_and_prompt_confirm_items_the_channel_pushed(store, event, monkeypatch):
    item = store.send("alice", "bob", "thanks")
    seen.claim(store, "bob")
    monkeypatch.setattr(delivery, "now_ms", lambda: store.get(item.id).updated_at + 1)
    if event == "stop":
        hook_manager.on_stop({"session_id": "s1"})
    else:
        hook_manager.on_user_prompt_submit({"session_id": "s1", "prompt": "go on"})
    assert store.get(item.id).state == "confirmed"


def test_an_unconfirmed_item_is_shown_again_by_whichever_transport_claims_next(store, monkeypatch):
    item = store.send("alice", "bob", "review my branch")
    [pushed] = seen.claim(store, "bob")
    assert pushed.id == item.id
    delivered_at = store.get(item.id).updated_at
    monkeypatch.setattr(seen, "now_ms", lambda: delivered_at + W - 1)
    assert seen.claim(store, "bob") == []
    monkeypatch.setattr(seen, "now_ms", lambda: delivered_at + W)
    assert [i.id for i in seen.claim(store, "carol")] == []
    assert [i.id for i in seen.claim(store, "bob")] == [item.id]
    assert [s for s, _, _ in states(store, item.id)] == ["pending", "delivered", "pending", "delivered"]


def test_a_redelivered_ledger_write_is_shown_again_rather_than_closed_as_seen(store, monkeypatch):
    item = store.send("operator", "bob", "a comment", ref="sw:3:c1")
    assert [i.id for i in seen.claim(store, "bob")] == [item.id]
    monkeypatch.setattr(seen, "now_ms", lambda: store.get(item.id).updated_at + W)
    assert [i.id for i in seen.claim(store, "bob")] == [item.id]
    assert store.get(item.id).state == "delivered"


def test_a_ledger_write_the_ledger_already_showed_still_closes_as_seen(store):
    seen.SeenMarks(store.redis).mark("bob", "sw:3:c1")
    item = store.send("operator", "bob", "a comment", ref="sw:3:c1")
    assert seen.claim(store, "bob") == []
    assert states(store, item.id)[-1] == ("done", "bob", "done: already shown through the ledger")


def test_the_hook_confirms_an_item_another_transport_delivered_instead_of_redelivering_it(store, capsys, monkeypatch):
    item = store.send("alice", "bob", "review my branch")
    seen.claim(store, "bob")
    monkeypatch.setattr(seen, "now_ms", lambda: store.get(item.id).updated_at + W)
    assert "review my branch" not in _pre(capsys)
    assert store.get(item.id).state == "confirmed"


def test_confirm_failures_are_logged_and_never_raise(monkeypatch):
    logged = []
    monkeypatch.setattr(delivery, "confirm_shown", lambda session_id: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(hook_manager, "log", lambda message, data=None: logged.append((message, data)))
    hook_manager._confirm_inbox("s1")
    assert logged == [("inbox confirmation failed", {"error": "down"})]


def test_the_wake_ladder_treats_a_redelivered_item_as_pending(store):
    class Herdr:
        prompts = []

        def agent_status(self, agent):
            return "idle"

        def typed_input(self, agent):
            return ""

        def prompt(self, agent, text):
            self.prompts.append(agent.pane_id)

    agents = [
        AgentRecord("bob", "eng", "t1", pane_id="p1", harness="codex"),
        AgentRecord("boss", MASTER, MASTER, pane_id="pm"),
    ]
    item = delivered(store)
    actions = wake.wake_pass(store, "sw", agents, Herdr(), None, item.updated_at + W, wake.DEFAULT_WINDOW_S * 1000)
    assert actions[0] == f"redelivered message {item.id}"
    assert f"woke bob for message {item.id}" in actions
    assert store.get(item.id).state == "pending"


def test_a_failed_channel_write_puts_every_unsent_item_back_to_pending(store):
    import anyio

    first, second = store.send("alice", "bob", "one"), store.send("alice", "bob", "two")

    class Broken:
        async def send(self, message):
            raise anyio.BrokenResourceError

    async def drive():
        ready = anyio.Event()
        ready.set()
        pubsub = store.redis.pubsub()
        with pytest.raises(anyio.BrokenResourceError):
            await channel._push(store, "bob", Broken(), ready, pubsub)

    channel.SETTLE_S, saved = 0, channel.SETTLE_S
    try:
        anyio.run(drive)
    finally:
        channel.SETTLE_S = saved
    assert (store.get(first.id).state, store.get(second.id).state) == ("pending", "pending")
    assert states(store, first.id)[-1] == ("pending", "bob", "the inbox channel could not show it")


def test_a_cancelled_channel_write_puts_the_item_back_to_pending(store):
    import anyio

    item = store.send("alice", "bob", "one")

    class Cancelled(BaseException):
        pass

    class Stopping:
        async def send(self, message):
            raise Cancelled

    async def drive():
        ready = anyio.Event()
        ready.set()
        with pytest.raises(Cancelled):
            await channel._push(store, "bob", Stopping(), ready, store.redis.pubsub())

    channel.SETTLE_S, saved = 0, channel.SETTLE_S
    try:
        anyio.run(drive)
    finally:
        channel.SETTLE_S = saved
    assert states(store, item.id)[-1] == ("pending", "bob", "the inbox channel could not show it")


def test_pending_context_is_empty_for_a_session_with_no_identity(store, monkeypatch):
    store.send("alice", "bob", "hi")

    def nobody(env):
        raise InboxError("no identity")

    monkeypatch.setattr(delivery, "identity", nobody)
    assert delivery.pending_context("s1") == ""


def test_confirm_shown_connects_with_the_session_environment(store, monkeypatch):
    item = store.deliver(store.send("alice", "carol", "hi").id, "carol")
    seen_env = []
    monkeypatch.setattr(delivery, "connect", lambda environ=None: seen_env.append(environ) or store)
    monkeypatch.setattr(delivery, "now_ms", lambda: item.updated_at + 1)
    delivery.confirm_shown("s1", {"AGENTIHOOKS_AGENT_NAME": "carol"})
    assert seen_env == [{"AGENTIHOOKS_AGENT_NAME": "carol", "CLAUDE_CODE_SESSION_ID": "s1"}]
    assert store.get(item.id).state == "confirmed"


@pytest.mark.parametrize("event", ["stop", "prompt"])
def test_stop_and_prompt_confirm_for_their_own_session(event, monkeypatch):
    sessions = []
    monkeypatch.setattr(delivery, "confirm_shown", lambda session_id: sessions.append(session_id))
    if event == "stop":
        hook_manager.on_stop({"session_id": "s9"})
    else:
        hook_manager.on_user_prompt_submit({"session_id": "s9", "prompt": "go on"})
    assert sessions == ["s9"]


def test_a_seat_successor_still_closes_a_ledger_write_its_predecessor_was_shown(store):
    seat = "eng-1@sw"
    store.seats.occupy(seat, "carol", at=1)
    item = store.deliver(store.send("operator", seat, "a comment", ref="sw:3:c1").id, "carol")
    store.requeue(item.id, "inbox", "again")
    store.seats.occupy(seat, "bob", at=2)
    seen.SeenMarks(store.redis).mark("bob", "sw:3:c1")
    assert seen.claim(store, "bob") == []
    assert store.get(item.id).state == "done"


def test_confirm_shown_skips_items_it_cannot_confirm_and_goes_on(store, monkeypatch):
    other = store.deliver(store.send("alice", "carol", "for carol").id, "carol")
    store.redis.zadd(store.key("delivered"), {"gone": 1, "never-delivered": 2})
    store.redis.zadd(store.key("delivered"), {other.id: 3})
    stale = store.send("alice", "bob", "pending only")
    store.redis.zadd(store.key("delivered"), {stale.id: 4})
    closed = delivered(store, "closed")
    store.close(closed.id, "bob", "done", "handled the request")
    store.redis.zadd(store.key("delivered"), {closed.id: 5})
    store.seats.occupy("eng-1@sw", "bob", at=1)
    moved_on = store.deliver(store.send("alice", "eng-1@sw", "for the seat").id, "bob")
    store.seats.occupy("eng-1@sw", "carol", at=2)
    store.redis.zadd(store.key("delivered"), {moved_on.id: 5.5})
    with pytest.raises(InboxError):
        store.confirm(moved_on.id, "bob")
    mine = delivered(store, "mine")
    store.redis.zadd(store.key("delivered"), {mine.id: 6})
    assert [i.id for i in store.confirm_shown("bob", 7)] == [mine.id]


def test_confirm_shown_leaves_an_item_with_no_delivery_on_record(store):
    item = store.deliver(store.send("alice", "XXXX", "hi").id, "XXXX")
    store.redis.delete(store.key("history", item.id))
    assert store.confirm_shown("XXXX", item.updated_at + 1) == []
    assert store.get(item.id).state == "delivered"


def test_a_wake_entry_after_the_delivery_does_not_hide_an_earlier_one(store, monkeypatch):
    item = store.send("operator", "bob", "a comment", ref="sw:3:c1")
    seen.claim(store, "bob")
    monkeypatch.setattr(seen, "now_ms", lambda: store.get(item.id).updated_at + W)
    real = store.deliver

    def deliver_then_wake(item_id, receiver):
        moved = real(item_id, receiver)
        store.redis.rpush(store.key("history", item_id), '{"event": "woke", "by": "swarm"}')
        return moved

    monkeypatch.setattr(store, "deliver", deliver_then_wake)
    assert [i.id for i in seen.claim(store, "bob")] == [item.id]


def test_leaving_delivered_drops_the_item_from_the_delivered_index(store):
    read, moved = delivered(store), delivered(store, "moved")
    store.read(read.id, "bob")
    store.redirect(moved.id, "swarm", "carol", "reassigned")
    assert store.redis.zrange(store.key("delivered"), 0, -1) == []


def test_redelivery_leaves_an_item_delivered_again_since_its_index_entry(store):
    item = delivered(store)
    store.redis.zadd(store.key("delivered"), {item.id: item.updated_at - W})
    assert store.redeliver(item.updated_at + 1, W) == []
    assert store.get(item.id).state == "delivered"


def test_requeue_stamps_its_time_and_puts_the_address_back_on_the_waiting_set(store, monkeypatch):
    import scripts.inbox.store as store_module

    store.pending()
    item = delivered(store)
    monkeypatch.setattr(store_module, "now_ms", lambda: 123456)
    moved = store.requeue(item.id, "inbox", "again")
    assert (moved.updated_at, store.get(item.id).updated_at) == (123456, 123456)
    assert store.redis.smembers(store.key("waiting")) == {"bob"}
    assert [i.id for i in store.pending()] == [item.id]


def test_requeue_retries_a_watch_conflict_and_then_names_the_message(store, monkeypatch):
    from redis.exceptions import WatchError

    item = delivered(store)
    real, calls = store._try_requeue, []

    def flaky(*args):
        calls.append(args)
        if len(calls) == 1:
            raise WatchError
        return real(*args)

    monkeypatch.setattr(store, "_try_requeue", flaky)
    assert store.requeue(item.id, "inbox", "again").state == "pending"
    assert len(calls) == 2

    def conflict(*args):
        raise WatchError

    monkeypatch.setattr(store, "_try_requeue", conflict)
    with pytest.raises(InboxError) as raised:
        store.requeue(item.id, "inbox", "again")
    assert str(raised.value) == f"message {item.id} changed meanwhile; run the command again"
