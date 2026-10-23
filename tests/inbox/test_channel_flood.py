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
    session, pushed = Session(), set()
    item = inbox.send("alice", "bob", "the stalled session holds this one")
    _pass(inbox, session, pushed)
    inbox.redeliver(now_ms() + 1, 0)
    assert inbox.get(item.id).state == "pending"
    _pass(inbox, session, pushed)
    assert inbox.get(item.id).state == "delivered"
    inbox.close(item.id, "bob", "done", "done: handled")
    _pass(inbox, session, pushed)
    assert session.shown == [item.id]


def test_a_burst_waits_pending_while_the_session_holds_ten_unconfirmed_pushes(channel_backend):
    _, inbox = channel_backend
    session, pushed = Session(), set()
    items = [inbox.send("alice", "bob", f"burst {n}") for n in range(25)]
    _pass(inbox, session, pushed)
    assert session.shown == [item.id for item in items[:5]]
    for _ in range(4):
        _pass(inbox, session, pushed)
    assert session.shown == [item.id for item in items[:10]]
    assert {inbox.get(item.id).state for item in items[10:]} == {"pending"}
    inbox.confirm(items[0].id, "bob")
    inbox.redis.delete(inbox.key("item", items[1].id))
    _pass(inbox, session, pushed)
    assert session.shown == [item.id for item in items[:12]]
    assert inbox.get(items[12].id).state == "pending"
