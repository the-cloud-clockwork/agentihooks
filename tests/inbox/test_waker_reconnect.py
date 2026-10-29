import queue
import threading
import time
from types import SimpleNamespace

import pytest

from scripts.inbox import wake, waker
from scripts.inbox.store import InboxStore, now_ms
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.usefixtures("recovery_deadline")


@pytest.fixture
def backend():
    import fakeredis

    server = fakeredis.FakeServer()
    return SimpleNamespace(
        server=server,
        client=fakeredis.FakeRedis(server=server, decode_responses=True),
        polling=threading.Event(),
        retrying=threading.Event(),
        restored=threading.Event(),
        stopped=threading.Event(),
        prompts=queue.Queue(),
        errors=queue.Queue(),
        subscriptions=[],
        sleep=time.sleep,
    )


def subscription_factory(backend):
    real_pubsub = backend.client.pubsub

    class Subscription:
        def __init__(self):
            self.inner = real_pubsub()
            self.closed = False
            backend.subscriptions.append(self)

        def subscribe(self, name):
            self.inner.subscribe(name)

        def get_message(self, timeout):
            if backend.stopped.is_set():
                raise KeyboardInterrupt
            backend.polling.set()
            return self.inner.get_message(timeout=timeout)

        def close(self):
            self.closed = True
            self.inner.close()

    return Subscription


class Herdr:
    def __init__(self, prompts):
        self.prompts = prompts

    def agent_status(self, agent):
        return "idle"

    def typed_input(self, agent):
        return ""

    def prompt(self, agent, text):
        self.prompts.put((agent.pane_id, text))


def restore_during_run(backend, inbox):
    try:
        assert backend.polling.wait(3), "waker never subscribed"
        inbox.send("alice", "bob", "before outage")
        assert backend.prompts.get(timeout=3) == ("p1", wake.WAKE_TEXT)
        backend.server.connected = False
        assert backend.retrying.wait(3), "waker died instead of retrying"
        backend.server.connected = True
        inbox.send("alice", "bob", "missed during outage")
        backend.restored.set()
        assert backend.prompts.get(timeout=3) == ("p1", wake.WAKE_TEXT)
    except BaseException as exc:
        backend.errors.put(exc)
    finally:
        backend.server.connected = True
        backend.stopped.set()
        backend.restored.set()


def retry_sleep(backend, delay):
    if delay >= 1:
        assert delay == 1
        backend.retrying.set()
        assert backend.restored.wait(3), "Redis recovery was never released"
    else:
        backend.sleep(delay)


def test_running_waker_wakes_pending_items_after_redis_returns(backend, monkeypatch):
    store, inbox = RedisStore(backend.client), InboxStore(backend.client)
    store.create(SwarmConfig("sw", "/repo", 1, 1, state="running"))
    store.put_agent("sw", AgentRecord("bob", "eng", "t1", pane_id="p1", harness="codex"))
    monkeypatch.setattr(backend.client, "pubsub", subscription_factory(backend))
    monkeypatch.setattr(waker, "RECHECK_S", 0.02)
    monkeypatch.setattr(time, "sleep", lambda delay: retry_sleep(backend, delay))
    driver = threading.Thread(target=restore_during_run, args=(backend, inbox))
    driver.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            waker.run(store, Herdr(backend.prompts), now_ms, {})
    finally:
        backend.server.connected = True
        backend.stopped.set()
        backend.restored.set()
        driver.join(4)
    assert not driver.is_alive(), "outage driver did not stop"
    assert backend.errors.empty(), list(backend.errors.queue)
    assert all(subscription.closed for subscription in backend.subscriptions)


def test_waker_reconnect_backoff_caps_and_rechecks_without_a_notification(backend, monkeypatch):
    store, inbox = RedisStore(backend.client), InboxStore(backend.client)
    store.create(SwarmConfig("sw", "/repo", 1, 1, state="running"))
    store.put_agent("sw", AgentRecord("bob", "eng", "t1", pane_id="p1", harness="codex"))
    backend.server.connected = False
    delays = []

    def sleep(delay):
        delays.append(delay)
        if len(delays) == 7:
            backend.server.connected = True
            inbox.send("alice", "bob", "pending at restore")

    def prompt(self, agent, text):
        backend.prompts.put((agent.pane_id, text))
        backend.stopped.set()

    monkeypatch.setattr(backend.client, "pubsub", subscription_factory(backend))
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(Herdr, "prompt", prompt)
    try:
        with pytest.raises(KeyboardInterrupt):
            waker.run(store, Herdr(backend.prompts), now_ms, {})
    finally:
        backend.server.connected = True
    assert delays == [1, 2, 4, 8, 16, 30, 30]
    assert backend.prompts.get_nowait() == ("p1", wake.WAKE_TEXT)
    assert all(subscription.closed for subscription in backend.subscriptions)
