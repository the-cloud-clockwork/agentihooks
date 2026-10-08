"""The inbox as a Claude Code channel: an MCP server that pushes every inbox item for this session into the open
session the moment it lands, idle or busy, with a reply tool that answers it like msg reply.

A swarm Claude launch names it with --mcp-config and the development channels flag (launch_args). Each push claims
its item, so the next tool call hook never shows it again; the hook and the tick wake stay the path for a session
that runs without the channel.
"""

import json
import sys
from pathlib import Path

from scripts.inbox.cli import check_for_operator, identity
from scripts.inbox.seen import claim
from scripts.inbox.store import NOTIFY, InboxError, connect

NAME = "inbox"
FLAG = f"--dangerously-load-development-channels=server:{NAME}"
WARNING = "I am using this for local development"
ROOT = str(Path(__file__).resolve().parents[2])
RECHECK_S = 5.0
SETTLE_S = 1.0
METHOD = "notifications/claude/channel"
UNSHOWN = "the inbox channel could not show it"


def launch_args(root=ROOT, python=sys.executable):
    code = f"import sys; sys.path.insert(0, {root!r}); from scripts.inbox.channel import main; main()"
    config = {"mcpServers": {NAME: {"command": python, "args": ["-I", "-c", code]}}}
    return [f"--mcp-config={json.dumps(config)}", FLAG]


def event(item):
    meta = {"item_id": item.id, "sender": item.sender, "sent_at_ms": str(item.created_at)}
    return {"method": METHOD, "params": {"content": item.text, "meta": meta}}


def answer(store, me, item_id, text):
    try:
        check_for_operator(store.get(item_id).sender, text)
        sent = store.reply(item_id, me, text)
    except InboxError as exc:
        return f"not sent: {exc}"
    return f"sent to {sent.address} as message {sent.id}; message {item_id} is closed"


def _instructions(me):
    return (
        f'Inbox items for {me} arrive as <channel source="{NAME}" item_id="..." sender="..." sent_at_ms="...">. '
        "Act on the text. Answer one with the reply tool, passing item_id from the tag; close one that needs no "
        "answer with agentihooks msg close <item_id> done."
    )


def _tool():
    import mcp.types as types

    return types.Tool(
        name="reply",
        description="Answer an inbox item: sends the text to its sender and closes the item done",
        inputSchema={
            "type": "object",
            "properties": {"item_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["item_id", "text"],
        },
    )


async def _recheck(store, me, pubsub):
    import anyio

    finished = anyio.Event()

    async def wait():
        await anyio.to_thread.run_sync(lambda: pubsub.get_message(timeout=RECHECK_S))
        finished.set()

    async with anyio.create_task_group() as group:
        group.start_soon(wait)
        try:
            await finished.wait()
        finally:
            if not finished.is_set():
                store.redis.publish(NOTIFY, me)


async def _push(store, me, write, ready, pubsub):
    import anyio
    import mcp.types as types
    from mcp.shared.message import SessionMessage

    try:
        await ready.wait()
        await anyio.sleep(SETTLE_S)
        while True:
            items = claim(store, me)
            for index, item in enumerate(items):
                note = types.JSONRPCNotification(jsonrpc="2.0", **event(item))
                try:
                    await write.send(SessionMessage(types.JSONRPCMessage(note)))
                except BaseException:
                    for unsent in items[index:]:
                        store.requeue(unsent.id, me, UNSHOWN)
                    raise
            await _recheck(store, me, pubsub)
    finally:
        pubsub.close()


async def run(store, me, read, write):
    import anyio
    import mcp.types as types
    from mcp.server.lowlevel import Server

    server, ready = Server(NAME, instructions=_instructions(me)), anyio.Event()

    @server.list_tools()
    async def list_tools():
        ready.set()
        return [_tool()]

    @server.call_tool()
    async def call_tool(name, arguments):
        return [types.TextContent(type="text", text=answer(store, me, arguments["item_id"], arguments["text"]))]

    pubsub = store.redis.pubsub()
    pubsub.subscribe(NOTIFY)
    options = server.create_initialization_options(experimental_capabilities={"claude/channel": {}})
    async with anyio.create_task_group() as group:
        group.start_soon(_push, store, me, write, ready, pubsub)
        await server.run(read, write, options)
        group.cancel_scope.cancel()


async def serve(store, me):
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read, write):
        await run(store, me, read, write)


def main():
    import anyio

    try:
        me, store = identity(), connect()
    except InboxError as exc:
        print(f"agentihooks inbox channel: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    anyio.run(serve, store, me)
