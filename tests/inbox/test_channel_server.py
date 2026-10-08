import contextlib
import queue
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from redis._parsers.encoders import Encoder

from scripts.inbox import channel
from scripts.inbox.store import NOTIFY, InboxError, Item

pytestmark = pytest.mark.xdist_group("mcp-sdk")


class FakePubSub:
    def __init__(self):
        self.notes, self.channels = queue.Queue(), []
        self.closed = False

    def subscribe(self, name):
        self.channels.append(name)
        self.notes.put({"type": "subscribe", "data": 1})

    def publish(self, name, data):
        if name in self.channels:
            self.notes.put({"type": "message", "data": data})

    def get_message(self, timeout):
        try:
            return self.notes.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self.closed = True


class FakeStore:
    """The inbox calls the channel server makes, in memory; the Redis store itself is covered in test_realtime."""

    def __init__(self):
        self.items, self.subs = {}, []
        self.redis = SimpleNamespace(pubsub=self._pubsub, publish=self._publish)

    def _pubsub(self):
        self.subs.append(FakePubSub())
        return self.subs[-1]

    def _publish(self, name, data):
        Encoder("utf-8", "strict", False).encode(data)
        for sub in self.subs:
            sub.publish(name, data)

    def send(self, sender, address, text, notify=True):
        at = time.time_ns() // 1_000_000
        item = Item(f"m{len(self.items) + 1}", sender, address, text, "pending", at, at)
        self.items[item.id] = item
        for sub in self.subs if notify else ():
            sub.publish(NOTIFY, address)
        return item

    def get(self, item_id):
        return self.items[item_id]

    def inbox(self, address):
        return [i for i in self.items.values() if i.address == address]

    def pending_mail(self, me):
        return [i for i in self.inbox(me) if i.state == "pending"]

    def deliver(self, item_id, me):
        if self.items[item_id].state != "pending":
            return None
        self.items[item_id] = replace(self.items[item_id], state="delivered")
        return self.items[item_id]

    def reply(self, item_id, replier, text):
        item = self.items[item_id]
        if item.address != replier:
            raise InboxError(f"message {item_id} belongs to {item.address}, not {replier}")
        answer = self.send(replier, item.sender, text)
        self.items[item_id] = replace(item, state="done")
        return answer


INSTRUCTIONS = (
    'Inbox items for bob arrive as <channel source="inbox" item_id="..." sender="..." sent_at_ms="...">. '
    "Act on the text. Answer one with the reply tool, passing item_id from the tag; close one that needs no "
    "answer with agentihooks msg close <item_id> done."
)
TOOL = {
    "name": "reply",
    "description": "Answer an inbox item: sends the text to its sender and closes the item done",
    "inputSchema": {
        "type": "object",
        "properties": {"item_id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["item_id", "text"],
    },
}


@pytest.fixture
def store():
    return FakeStore()


def _message(**fields):
    import mcp.types as types
    from mcp.shared.message import SessionMessage

    return SessionMessage(types.JSONRPCMessage.model_validate({"jsonrpc": "2.0", **fields}))


INITIALIZE = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}


def session(store, scenario):
    """Run the channel server for bob over memory streams; scenario(send, receive) drives it as Claude would."""
    import anyio

    async def drive():
        to_server, server_in = anyio.create_memory_object_stream(50)
        server_out, from_server = anyio.create_memory_object_stream(50)

        async def receive():
            got = await from_server.receive()
            return got.message.root.model_dump(exclude_none=True)

        async with anyio.create_task_group() as group:
            group.start_soon(channel.run, store, "bob", server_in, server_out)
            await to_server.send(_message(id=1, method="initialize", params=INITIALIZE))
            opened = await receive()
            await to_server.send(_message(method="notifications/initialized"))
            await to_server.send(_message(id=2, method="tools/list"))
            listed = await receive()
            result = await scenario(to_server.send, receive)
            group.cancel_scope.cancel()
        return opened, listed, result

    return anyio.run(drive)


def test_the_server_declares_the_channel_its_instructions_and_the_reply_tool(store, monkeypatch):
    monkeypatch.setattr(channel, "SETTLE_S", 0)

    async def nothing(send, receive):
        return None

    opened, listed, _ = session(store, nothing)
    assert opened["result"]["capabilities"]["experimental"] == {"claude/channel": {}}
    assert opened["result"]["serverInfo"]["name"] == "inbox"
    assert opened["result"]["instructions"] == INSTRUCTIONS
    assert listed["result"] == {"tools": [TOOL]}


def _push_notified_item(store, monkeypatch, delay=0, notify=True):
    import anyio

    entered, notified = threading.Event(), threading.Event()
    real_get_message = FakePubSub.get_message

    def get_message(pubsub, timeout):
        if not pubsub.notes.empty():
            return real_get_message(pubsub, timeout)
        assert timeout == 3600
        entered.set()
        note = real_get_message(pubsub, 60)
        if note is not None and note["type"] == "message":
            notified.set()
        return note

    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 3600)
    monkeypatch.setattr(FakePubSub, "get_message", get_message)

    async def scenario(send, receive):
        assert await anyio.to_thread.run_sync(lambda: entered.wait(30)), "session never entered the recheck wait"
        item = store.send("alice", "bob", "hello", notify=notify)
        with anyio.fail_after(10):
            pushed = await receive()
            assert notified.is_set(), "item arrived without interrupting the recheck wait"
            await send(
                _message(
                    id=3, method="tools/call", params={"name": "reply", "arguments": {"item_id": item.id, "text": "hi"}}
                )
            )
            return item, pushed, await receive()

    if delay:
        time.sleep(delay)
    _, _, (item, pushed, replied) = session(store, scenario)
    assert pushed == {"jsonrpc": "2.0", **channel.event(store.get(item.id))}
    [answer] = store.inbox("alice")
    assert replied["result"]["content"] == [
        {"type": "text", "text": f"sent to alice as message {answer.id}; message {item.id} is closed"}
    ]
    assert store.get(item.id).state == "done" and answer.text == "hi"


@pytest.mark.parametrize("delay", [0, 3])
def test_an_item_is_pushed_at_its_notify_and_answered_through_the_reply_tool(store, monkeypatch, delay):
    _push_notified_item(store, monkeypatch, delay=delay)


def test_notification_proof_rejects_a_missing_notify(store, monkeypatch):
    with pytest.raises(ExceptionGroup) as caught:
        _push_notified_item(store, monkeypatch, notify=False)
    assert len(caught.value.exceptions) == 1
    assert isinstance(caught.value.exceptions[0], TimeoutError)


def test_an_item_whose_notify_was_missed_arrives_at_the_next_recheck(store, monkeypatch):
    import anyio

    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 0.3)

    async def scenario(send, receive):
        await anyio.sleep(0.2)
        item = store.send("alice", "bob", "quiet", notify=False)
        with anyio.fail_after(2):
            return item, await receive()

    _, _, (item, pushed) = session(store, scenario)
    assert pushed["params"]["meta"]["item_id"] == item.id


def test_items_waiting_before_the_session_opened_arrive_once_it_lists_its_tools(store, monkeypatch):
    import anyio

    monkeypatch.setattr(channel, "SETTLE_S", 0)
    first = store.send("alice", "bob", "one")
    second = store.send("alice", "bob", "two")

    async def scenario(send, receive):
        with anyio.fail_after(2):
            return [(await receive())["params"]["meta"]["item_id"] for _ in range(2)]

    assert session(store, scenario)[2] == [first.id, second.id]


def _close_idle_session(store, monkeypatch, delay=0, wake=True):
    import anyio

    entered, worker_stopped = threading.Event(), threading.Event()
    closed, close_requested = threading.Event(), threading.Event()
    outcomes = queue.Queue()
    real_get_message = FakePubSub.get_message

    def get_message(pubsub, timeout):
        if not pubsub.notes.empty():
            return real_get_message(pubsub, timeout)
        entered.set()
        try:
            assert timeout == 3600
            return real_get_message(pubsub, 60)
        finally:
            worker_stopped.set()

    async def idle(send, receive):
        await anyio.to_thread.run_sync(close_requested.wait)

    def drive():
        try:
            if delay:
                time.sleep(delay)
            session(store, idle)
            outcomes.put(None)
        except BaseException as error:
            outcomes.put(error)
        finally:
            closed.set()

    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 3600)
    monkeypatch.setattr(FakePubSub, "get_message", get_message)
    if not wake:
        monkeypatch.setattr(store.redis, "publish", lambda *args: None)
    driver = threading.Thread(target=drive)
    driver.start()
    try:
        assert entered.wait(30), "session never entered the recheck wait"
        close_requested.set()
        interrupted = closed.wait(10)
        stopped_on_close = worker_stopped.is_set()
    finally:
        close_requested.set()
        for pubsub in store.subs:
            pubsub.notes.put({"type": "message", "data": "cleanup"})
        driver.join(30)
    assert not driver.is_alive(), "session worker survived cleanup"
    assert worker_stopped.wait(30), "recheck worker survived cleanup"
    error = outcomes.get_nowait()
    if error is not None:
        raise error
    assert interrupted, "close did not interrupt the recheck wait"
    assert stopped_on_close, "recheck worker survived close"
    assert all(pubsub.closed for pubsub in store.subs), "close left a subscription open"


@pytest.mark.parametrize("delay", [0, 2.5], ids=["normal", "delayed-scheduling"])
def test_an_idle_session_waits_on_redis_and_closes_without_waiting_out_the_recheck(store, monkeypatch, delay):
    _close_idle_session(store, monkeypatch, delay)


def test_idle_close_proof_rejects_a_missing_close_wake(store, monkeypatch):
    with pytest.raises(AssertionError, match="close did not interrupt the recheck wait"):
        _close_idle_session(store, monkeypatch, wake=False)


def test_serve_runs_the_session_over_stdio(store, monkeypatch):
    import mcp.server.stdio

    seen = []

    @contextlib.asynccontextmanager
    async def stdio():
        yield "read", "write"

    async def run(*args):
        seen.append(args)

    monkeypatch.setattr(mcp.server.stdio, "stdio_server", stdio)
    monkeypatch.setattr(channel, "run", run)
    import anyio

    anyio.run(channel.serve, store, "bob")
    assert seen == [(store, "bob", "read", "write")]


def test_main_serves_this_sessions_identity(store, monkeypatch):
    import anyio

    ran = []
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    monkeypatch.setattr(channel, "connect", lambda: store)
    monkeypatch.setattr(anyio, "run", lambda *args: ran.append(args))
    channel.main()
    assert ran == [(channel.serve, store, "bob")]


def test_main_without_redis_exits_with_the_reason(monkeypatch, capsys):
    def down():
        raise InboxError("Redis is unreachable")

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    monkeypatch.setattr(channel, "connect", down)
    with pytest.raises(SystemExit) as stopped:
        channel.main()
    assert stopped.value.code == 1
    assert capsys.readouterr().err == "agentihooks inbox channel: Redis is unreachable\n"
