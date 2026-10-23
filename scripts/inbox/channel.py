"""The inbox as a Claude Code channel: an MCP server that pushes every inbox item for this session into the open
session the moment it lands, idle or busy, with a reply tool that answers it like msg reply.

A swarm Claude launch names it with --mcp-config and the development channels flag (launch_args). Each push claims
its item, so the next tool call hook never shows it again; the hook and the tick wake stay the path for a session
that runs without the channel.
"""

import json
import sys
import time
from pathlib import Path

from scripts.inbox.bus import FeedError, feed_for
from scripts.inbox.cli import check_answer, identity
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


def launch_args(root: str = ROOT, python: str = sys.executable, *, active: bool = True) -> list[str]:
    entry = "main()" if active else "main(active=False)"
    code = f"import sys; sys.path.insert(0, {root!r}); from scripts.inbox.channel import main; {entry}"
    config = {"mcpServers": {NAME: {"command": python, "args": ["-I", "-c", code]}}}
    return [f"--mcp-config={json.dumps(config)}", FLAG]


def register(environ: dict[str, str]) -> None:
    from scripts import claude_trust
    from scripts.claude_config import claude_json

    config = claude_json(environ)
    config.parent.mkdir(parents=True, exist_ok=True)
    lock = config.with_name(config.name + ".lock")
    deadline = time.monotonic() + 3
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OSError(f"{lock} is held") from None
            time.sleep(0.1)
    try:
        data = claude_trust._read(config)
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError(f"{config} mcpServers is not a JSON object")
        server = json.loads(launch_args(active=False)[0].removeprefix("--mcp-config="))["mcpServers"][NAME]
        if servers.get(NAME) != server:
            servers[NAME] = server
            claude_trust._write(config, data)
    finally:
        lock.rmdir()


def event(item):
    meta = {"item_id": item.id, "sender": item.sender, "sent_at_ms": str(item.created_at)}
    content = f"Information only; closes on read or confirmed receipt.\n{item.text}" if item.fyi else item.text
    return {"method": METHOD, "params": {"content": content, "meta": meta}}


def answer(store, me, item_id, text):
    try:
        check_answer(store, item_id, store.get(item_id).sender, text)
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
    from redis.exceptions import RedisError

    finished, error = anyio.Event(), None

    async def wait():
        nonlocal error
        try:
            await anyio.to_thread.run_sync(lambda: pubsub.get_message(timeout=RECHECK_S))
        except RedisError as exc:
            error = exc
        finally:
            finished.set()

    async with anyio.create_task_group() as group:
        group.start_soon(wait)
        try:
            await finished.wait()
        finally:
            if not finished.is_set():
                try:
                    store.redis.publish(NOTIFY, me)
                except RedisError:
                    pass
    if error is not None:
        raise error


async def _deliver(store, me, write):
    import mcp.types as types
    from mcp.shared.message import SessionMessage

    items = claim(store, me)
    for index, item in enumerate(items):
        note = types.JSONRPCNotification(jsonrpc="2.0", **event(item))
        try:
            await write.send(SessionMessage(types.JSONRPCMessage(note)))
        except BaseException:
            for unsent in items[index:]:
                store.requeue(unsent.id, me, UNSHOWN)
            raise


async def _push(store, me, write, ready):
    import anyio
    from redis.exceptions import RedisError

    pubsub, delay = None, 1.0
    try:
        await ready.wait()
        await anyio.sleep(SETTLE_S)
        while True:
            try:
                if pubsub is None:
                    pubsub = store.redis.pubsub()
                    pubsub.subscribe(NOTIFY)
                await _deliver(store, me, write)
                await _recheck(store, me, pubsub)
                delay = 1.0
            except RedisError:
                if pubsub is not None:
                    pubsub.close()
                    pubsub = None
                await anyio.sleep(delay)
                delay = min(delay * 2, 30.0)
    finally:
        if pubsub is not None:
            pubsub.close()


async def _pull(store, me, write, ready, feed):
    import anyio
    from redis.exceptions import RedisError

    delay = 1.0
    try:
        await ready.wait()
        await anyio.sleep(SETTLE_S)
        while True:
            try:
                await _deliver(store, me, write)
                await anyio.to_thread.run_sync(feed.settle)
                await anyio.to_thread.run_sync(feed.wait, int(RECHECK_S * 1000))
                delay = 1.0
            except (RedisError, FeedError):
                await anyio.sleep(delay)
                delay = min(delay * 2, 30.0)
    finally:
        feed.close()


async def run(store, me, read, write, active=True, feed=None):
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
        if store is None:
            text = "not sent: Redis is unavailable; retry once it returns"
        else:
            text = answer(store, me, arguments["item_id"], arguments["text"])
        return [types.TextContent(type="text", text=text)]

    async def push():
        nonlocal store
        delay = 1.0
        while store is None:
            try:
                store = await anyio.to_thread.run_sync(connect)
            except InboxError:
                await anyio.sleep(delay)
                delay = min(delay * 2, 30.0)
        pulled = feed if feed is not None else feed_for(store, me)
        if pulled is None:
            await _push(store, me, write, ready)
        else:
            await _pull(store, me, write, ready, pulled)

    options = server.create_initialization_options(experimental_capabilities={"claude/channel": {}})
    async with anyio.create_task_group() as group:
        if active:
            group.start_soon(push)
        await server.run(read, write, options)
        group.cancel_scope.cancel()


async def serve(store, me, active=True):
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read, write):
        await run(store, me, read, write, active)


def main(active: bool = True) -> None:
    import anyio

    try:
        me = identity() if active else ""
    except InboxError as exc:
        print(f"agentihooks inbox channel: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    try:
        store = connect() if active else None
    except InboxError:
        store = None
    if active:
        anyio.run(serve, store, me)
    else:
        anyio.run(serve, store, me, False)
