import contextlib
import time

import pytest

from scripts.inbox import channel
from scripts.inbox import store as store_module
from scripts.inbox.store import InboxError, InboxStore

pytestmark = pytest.mark.xdist_group("fakeredis")

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
    import fakeredis

    return InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))


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


def test_an_item_is_pushed_at_its_notify_and_answered_through_the_reply_tool(store, monkeypatch):
    import anyio

    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 3.0)

    async def scenario(send, receive):
        await anyio.sleep(0.3)
        start = time.monotonic()
        item = store.send("alice", "bob", "hello")
        pushed = await receive()
        took = time.monotonic() - start
        await send(
            _message(
                id=3, method="tools/call", params={"name": "reply", "arguments": {"item_id": item.id, "text": "hi"}}
            )
        )
        return item, pushed, took, await receive()

    start = time.monotonic()
    _, _, (item, pushed, took, replied) = session(store, scenario)
    assert time.monotonic() - start < 2.5 and took < 1.0
    assert pushed == {"jsonrpc": "2.0", **channel.event(store.get(item.id))}
    [answer] = store.inbox("alice")
    assert replied["result"]["content"] == [
        {"type": "text", "text": f"sent to alice as message {answer.id}; message {item.id} is closed"}
    ]
    assert store.get(item.id).state == "done" and answer.text == "hi"


def test_an_item_whose_notify_was_missed_arrives_at_the_next_recheck(store, monkeypatch):
    import anyio

    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 0.3)

    async def scenario(send, receive):
        await anyio.sleep(0.2)
        monkeypatch.setattr(store_module, "NOTIFY", "elsewhere")
        item = store.send("alice", "bob", "quiet")
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
    monkeypatch.setattr(channel, "connect", lambda env: store if env["AGENTIHOOKS_AGENT_NAME"] == "bob" else None)
    monkeypatch.setattr(anyio, "run", lambda *args: ran.append(args))
    channel.main()
    assert ran == [(channel.serve, store, "bob")]


def test_main_without_redis_exits_with_the_reason(monkeypatch, capsys):
    def down(env):
        raise InboxError("Redis is unreachable")

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    monkeypatch.setattr(channel, "connect", down)
    with pytest.raises(SystemExit) as stopped:
        channel.main()
    assert stopped.value.code == 1
    assert capsys.readouterr().err == "agentihooks inbox channel: Redis is unreachable\n"
