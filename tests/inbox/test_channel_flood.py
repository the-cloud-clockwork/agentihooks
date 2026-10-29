import pytest

from scripts.inbox import channel
from scripts.inbox.store import now_ms

pytestmark = [pytest.mark.xdist_group("mcp-sdk"), pytest.mark.usefixtures("mcp_sdk")]


class Session:
    def __init__(self):
        self.shown = []

    async def send(self, message):
        self.shown.append(message.message.root.params["meta"]["item_id"])


def _pass(inbox, session, pushed):
    import anyio

    anyio.run(channel._deliver, inbox, "bob", session, pushed)


def test_a_running_channel_never_pushes_an_item_into_its_session_again(channel_backend):
    _, inbox = channel_backend
    session, pushed = Session(), channel.Pushed()
    item = inbox.send("alice", "bob", "the stalled session holds this one")
    _pass(inbox, session, pushed)
    inbox.redeliver(now_ms() + 1, 0)
    assert inbox.get(item.id).state == "pending"
    _pass(inbox, session, pushed)
    assert inbox.get(item.id).state == "delivered"
    assert pushed.held == {item.id}
    inbox.close(item.id, "bob", "done", "done: handled")
    _pass(inbox, session, pushed)
    assert session.shown == [item.id]
    assert pushed.held == set()


def test_an_item_that_leaves_the_inbox_and_returns_is_not_pushed_again(channel_backend):
    from scripts.inbox.store import InboxStore

    _, inbox = channel_backend
    elsewhere = InboxStore(inbox.redis, world="elsewhere")
    session, pushed = Session(), channel.Pushed()
    item = inbox.send("alice", "bob", "moves out and back")
    _pass(inbox, session, pushed)
    inbox.move_world(item.id, elsewhere, "inbox", "seat moved host")
    _pass(inbox, session, pushed)
    assert pushed.held == set()
    elsewhere.move_world(item.id, inbox, "inbox", "seat moved back")
    _pass(inbox, session, pushed)
    assert inbox.get(item.id).state == "delivered"
    assert session.shown == [item.id]


def test_a_burst_waits_pending_while_the_session_holds_ten_unconfirmed_pushes(channel_backend):
    _, inbox = channel_backend
    session, pushed = Session(), channel.Pushed()
    items = [inbox.send("alice", "bob", f"burst {n}") for n in range(25)]
    _pass(inbox, session, pushed)
    assert session.shown == [item.id for item in items[:5]]
    for _ in range(4):
        _pass(inbox, session, pushed)
    assert session.shown == [item.id for item in items[:10]]
    assert {inbox.get(item.id).state for item in items[10:]} == {"pending"}
    inbox.confirm(items[0].id, "bob")
    inbox.redis.delete(inbox.key("item", items[1].id))
    inbox.close(items[2].id, "bob", "done", "done: handled")
    _pass(inbox, session, pushed)
    assert session.shown == [item.id for item in items[:13]]
    assert inbox.get(items[13].id).state == "pending"
