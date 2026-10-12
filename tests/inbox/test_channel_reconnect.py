import pytest

from scripts.inbox import channel

pytestmark = [pytest.mark.xdist_group("mcp-sdk"), pytest.mark.usefixtures("recovery_deadline")]


def test_running_channel_rechecks_pending_items_after_redis_returns(channel_backend, monkeypatch):
    import anyio

    from tests.inbox.test_channel_server import _message, session

    server, inbox = channel_backend
    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 0.02)
    real_sleep = anyio.sleep

    async def scenario(send, receive):
        retrying, restored = anyio.Event(), anyio.Event()

        async def sleep(delay):
            if delay >= 1:
                retrying.set()
                await restored.wait()
            else:
                await real_sleep(delay)

        monkeypatch.setattr(anyio, "sleep", sleep)
        first = inbox.send("alice", "bob", "before outage")
        with anyio.fail_after(3):
            shown = await receive()
        assert shown["params"]["meta"]["item_id"] == first.id
        server.connected = False
        with anyio.fail_after(3):
            await retrying.wait()
            await send(_message(id=3, method="tools/list"))
            listed = await receive()
        assert listed["id"] == 3
        server.connected = True
        pending = inbox.send("alice", "bob", "missed during outage")
        restored.set()
        with anyio.fail_after(3):
            shown = await receive()
        assert shown["params"]["meta"]["item_id"] == pending.id

    try:
        session(inbox, scenario)
    finally:
        server.connected = True


def test_channel_replaces_a_failed_subscriber_and_resets_its_backoff(channel_backend, monkeypatch):
    import anyio
    from redis.exceptions import ConnectionError

    from tests.inbox.test_channel_server import session

    server, inbox = channel_backend
    subscriptions, reads = [], []
    real_pubsub, real_sleep = inbox.redis.pubsub, anyio.sleep
    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 0.02)

    def pubsub():
        subscription = real_pubsub()
        subscriptions.append(subscription)
        if len(subscriptions) == 1:

            def get_message(timeout):
                reads.append(timeout)
                if len(reads) == 1:
                    return None
                raise ConnectionError("subscriber disconnected")

            monkeypatch.setattr(subscription, "get_message", get_message)
        return subscription

    async def scenario(send, receive):
        retrying, resumed = anyio.Event(), anyio.Event()

        async def sleep(delay):
            if delay >= 1:
                assert delay == 1
                retrying.set()
                await resumed.wait()
            else:
                await real_sleep(delay)

        monkeypatch.setattr(anyio, "sleep", sleep)
        with anyio.fail_after(3):
            await retrying.wait()
        assert subscriptions[0].connection is None
        pending = inbox.send("alice", "bob", "waiting for a new subscriber")
        resumed.set()
        with anyio.fail_after(3):
            shown = await receive()
        assert shown["params"]["meta"]["item_id"] == pending.id
        assert len(subscriptions) == 2

    monkeypatch.setattr(inbox.redis, "pubsub", pubsub)
    session(inbox, scenario)
    assert all(subscription.connection is None for subscription in subscriptions)


def test_channel_opens_while_redis_is_down_and_delivers_after_restore(channel_backend, monkeypatch):
    import anyio
    from redis.exceptions import RedisError

    from scripts.inbox.store import InboxError
    from tests.inbox.test_channel_server import _message, session

    server, inbox = channel_backend
    server.connected = False
    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 0.02)

    def connect():
        try:
            inbox.redis.ping()
        except RedisError as exc:
            raise InboxError("Redis is unreachable") from exc
        return inbox

    monkeypatch.setattr(channel, "connect", connect)
    real_sleep = anyio.sleep

    async def scenario(send, receive):
        retrying, restored = anyio.Event(), anyio.Event()

        async def sleep(delay):
            if delay >= 1:
                retrying.set()
                await restored.wait()
            else:
                await real_sleep(delay)

        monkeypatch.setattr(anyio, "sleep", sleep)
        with anyio.fail_after(3):
            await retrying.wait()
            await send(
                _message(
                    id=3,
                    method="tools/call",
                    params={"name": "reply", "arguments": {"item_id": "unavailable", "text": "answer"}},
                )
            )
            refused = await receive()
        assert refused["result"]["content"][0]["text"] == "not sent: Redis is unavailable; retry once it returns"
        server.connected = True
        pending = inbox.send("alice", "bob", "queued before reconnect")
        restored.set()
        with anyio.fail_after(3):
            shown = await receive()
        assert shown["params"]["meta"]["item_id"] == pending.id
        await send(
            _message(
                id=4,
                method="tools/call",
                params={"name": "reply", "arguments": {"item_id": pending.id, "text": "received"}},
            )
        )
        with anyio.fail_after(3):
            answered = await receive()
        assert answered["result"]["content"][0]["text"] == (
            f"sent to alice as message {inbox.inbox('alice')[0].id}; message {pending.id} is closed"
        )

    try:
        session(None, scenario)
    finally:
        server.connected = True


def test_main_keeps_serving_when_redis_is_unavailable(monkeypatch):
    import anyio

    from scripts.inbox.store import InboxError

    def down():
        raise InboxError("Redis is unreachable")

    calls = []
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    monkeypatch.setattr(channel, "connect", down)
    monkeypatch.setattr(anyio, "run", lambda *args: calls.append(args))
    channel.main()
    assert calls == [(channel.serve, None, "bob")]


@pytest.mark.parametrize("startup", [False, True])
def test_channel_reconnect_backoff_caps_at_thirty_seconds(channel_backend, monkeypatch, startup):
    import anyio
    from redis.exceptions import RedisError

    from scripts.inbox.store import InboxError
    from tests.inbox.test_channel_server import session

    server, inbox = channel_backend
    server.connected = False
    delays, pending = [], []
    real_sleep = anyio.sleep
    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 0.02)

    def connect():
        try:
            inbox.redis.ping()
        except RedisError as exc:
            raise InboxError("Redis is unreachable") from exc
        return inbox

    async def sleep(delay):
        if delay >= 1:
            delays.append(delay)
            if len(delays) == 7:
                server.connected = True
                pending.append(inbox.send("alice", "bob", "after capped retry"))
        await real_sleep(0)

    async def scenario(send, receive):
        with anyio.fail_after(3):
            shown = await receive()
        assert shown["params"]["meta"]["item_id"] == pending[0].id

    monkeypatch.setattr(channel, "connect", connect)
    monkeypatch.setattr(anyio, "sleep", sleep)
    try:
        session(None if startup else inbox, scenario)
    finally:
        server.connected = True
    assert delays == [1, 2, 4, 8, 16, 30, 30]


def test_channel_closes_while_startup_reconnect_is_waiting(channel_backend, monkeypatch):
    import anyio

    from scripts.inbox.store import InboxError
    from tests.inbox.test_channel_server import session

    server, inbox = channel_backend
    server.connected = False
    monkeypatch.setattr(channel, "SETTLE_S", 0)

    def connect():
        raise InboxError("Redis is unreachable")

    async def scenario(send, receive):
        entered = anyio.Event()

        async def sleep(delay):
            entered.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(anyio, "sleep", sleep)
        with anyio.fail_after(3):
            await entered.wait()

    monkeypatch.setattr(channel, "connect", connect)
    try:
        session(None, scenario)
        assert server.connected is False
    finally:
        server.connected = True
