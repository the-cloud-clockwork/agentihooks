import argparse

import pytest

from scripts.inbox.dispatch import Dispatcher
from scripts.inbox.seen import SeenMarks, write_ref
from scripts.inbox.store import InboxStore

pytestmark = pytest.mark.xdist_group("fakeredis")

EVENT = {"rev": 3, "by": "operator", "kind": "note added", "id": "n-1", "text": "note 1"}


@pytest.fixture
def store():
    import fakeredis

    return InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))


@pytest.fixture
def dispatcher(store):
    found = Dispatcher(store)
    found.own("bob", "bridge-1")
    return found


def test_the_hook_delivery_shows_nothing_while_an_owner_holds_the_recipient(store, dispatcher, monkeypatch):
    from hooks.context import inbox_delivery

    item = store.send("alice", "bob", "hi")
    monkeypatch.setattr(inbox_delivery, "connect", lambda env: store)
    assert inbox_delivery.pending_context("s1", {"AGENTIHOOKS_AGENT_NAME": "bob"}) == ""
    assert store.get(item.id).state == "pending"
    dispatcher.release("bob", "bridge-1")
    shown = inbox_delivery.pending_context("s1", {"AGENTIHOOKS_AGENT_NAME": "bob"})
    assert shown == inbox_delivery._render(store.get(item.id))


def test_the_claude_channel_pushes_nothing_while_an_owner_holds_the_recipient(store, dispatcher, monkeypatch):
    import anyio

    from scripts.inbox import channel

    item = store.send("alice", "bob", "hi")
    sent = []

    class Stop(Exception):
        pass

    class Write:
        async def send(self, message):
            sent.append(message)

    class PubSub:
        def close(self):
            pass

    async def stop(*_):
        raise Stop

    async def push():
        ready = anyio.Event()
        ready.set()
        with pytest.raises(Stop):
            await channel._push(store, "bob", Write(), ready, PubSub())

    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "_recheck", stop)
    anyio.run(push)
    assert sent == []
    assert store.get(item.id).state == "pending"
    dispatcher.release("bob", "bridge-1")
    anyio.run(push)
    assert len(sent) == 1
    assert store.get(item.id).state == "delivered"


def test_the_ledger_hook_shows_nothing_while_an_owner_holds_the_recipient(store, dispatcher, monkeypatch):
    from scripts.swarm import store as swarm_store
    from scripts.swarm_ledger import ledger_hook

    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setattr(swarm_store, "redis_client", lambda env: store.redis)
    session = {"slug": "sw", "name": "bob"}
    assert ledger_hook.first_shown(session, [EVENT]) == []
    assert SeenMarks(store.redis).seen("bob", write_ref("sw", EVENT)) is False
    dispatcher.release("bob", "bridge-1")
    assert ledger_hook.first_shown(session, [EVENT]) == [EVENT]


def test_the_ledger_watch_prints_nothing_while_an_owner_holds_the_recipient(store, dispatcher, capsys):
    from scripts.swarm_ledger import watch_ledger

    args = argparse.Namespace(slug="sw", all=True, name="bob", since_rev=0)
    ledger = {"tasks": [], "_meta": {"rev": 3, "events": [EVENT], "members": {}}}
    watch_ledger.Watch(args, "path.json", SeenMarks(store.redis)).show(ledger)
    assert [line for line in capsys.readouterr().out.splitlines() if "note 1" in line] == []
    dispatcher.release("bob", "bridge-1")
    watch_ledger.Watch(args, "path.json", SeenMarks(store.redis)).show(ledger)
    assert len([line for line in capsys.readouterr().out.splitlines() if "note 1" in line]) == 1
