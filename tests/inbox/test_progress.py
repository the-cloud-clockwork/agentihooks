import json
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import scripts.inbox.channel as channel
from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord
from tests.inbox.test_channel_server import session
from tests.inbox.test_wake import FakeHerdr, FakeLedger

pytestmark = pytest.mark.xdist_group("mcp-sdk")


class StalledFeed:
    def __init__(self) -> None:
        self.calls = 0
        self.stalled = threading.Event()
        self.released = threading.Event()

    def settle(self) -> None:
        pass

    def wait(self, timeout_ms: int) -> list[Any]:
        self.calls += 1
        if self.calls > 1:
            self.stalled.set()
            assert self.released.wait(3)
        return []

    def close(self) -> None:
        self.released.set()


class BlockedWrite:
    def __init__(self, blocked: Any) -> None:
        self.blocked = blocked
        self.messages: list[Any] = []

    async def send(self, message: Any) -> None:
        import anyio

        if self.messages:
            self.blocked.set()
            await anyio.sleep_forever()
        self.messages.append(message)


def test_a_successful_delivery_is_recorded_before_the_next_write_blocks(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    import anyio

    _, inbox = channel_backend
    first = inbox.send("alice", "bob", "first delivery")
    second = inbox.send("alice", "bob", "blocked delivery")
    monkeypatch.setattr(channel, "SETTLE_S", 0)

    async def scenario() -> None:
        ready, blocked = anyio.Event(), anyio.Event()
        ready.set()
        write = BlockedWrite(blocked)
        async with anyio.create_task_group() as group:
            group.start_soon(channel._push, inbox, "bob", write, ready)
            try:
                with anyio.fail_after(3):
                    await blocked.wait()
                assert write.messages[0].message.root.params["meta"]["item_id"] == first.id
                status = json.loads(inbox.redis.get(inbox.key("channel-progress", "bob")))
                assert status["delivery_at"] >= status["ready_at"] > 0
                assert status["poll_at"] == 0
                assert inbox.redis.get(inbox.key("channel-live", "bob")) is None
            finally:
                group.cancel_scope.cancel()
        assert inbox.get(first.id).state == "delivered"
        assert inbox.get(second.id).state == "pending"

    anyio.run(scenario)


def test_delivery_progress_suppresses_wake_until_the_stalled_loops_lease_expires(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    import anyio

    _, inbox = channel_backend
    feed = StalledFeed()
    monkeypatch.setattr(channel, "SETTLE_S", 0)
    first = inbox.send("alice", "bob", "confirm channel delivery")
    agent = AgentRecord("bob", "eng", "t1", pane_id="p1", harness="claude")
    herdr = FakeHerdr({"p1": "idle"})
    ledger = FakeLedger()

    async def scenario(
        send: Callable[[Any], Awaitable[None]], receive: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        try:
            with anyio.fail_after(3):
                shown = await receive()
                assert await anyio.to_thread.run_sync(feed.stalled.wait, 2)
            assert shown["params"]["meta"]["item_id"] == first.id
            status = json.loads(inbox.redis.get(inbox.key("channel-progress", "bob")) or "{}")
            assert status.get("ready_at", 0) > 0
            assert status.get("poll_at", 0) >= status["ready_at"]
            assert status.get("delivery_at", 0) >= status["ready_at"]
            assert status.get("owner")
            live_key = inbox.key("channel-live", "bob")
            assert 0 < inbox.redis.ttl(live_key) <= 15
            inbox.confirm(first.id, "bob")
            pending = inbox.send("alice", "bob", "reply after the loop stalls")
            now = pending.created_at + 300_000
            wake.wake_pass(inbox, "sw", [agent], herdr, ledger, now, 300_000)
            assert herdr.prompts == []
            inbox.redis.expire(live_key, 0)
            wake.wake_pass(inbox, "sw", [agent], herdr, ledger, now + 1, 300_000)
            assert herdr.prompts == [("p1", wake.WAKE_TEXT)]
            assert inbox.get(pending.id).state == "pending"
        finally:
            feed.released.set()

    session(inbox, scenario, feed=feed)
