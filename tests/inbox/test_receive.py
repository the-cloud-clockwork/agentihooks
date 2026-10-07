import threading
from concurrent.futures import ThreadPoolExecutor

import fakeredis
import pytest

from scripts.inbox.store import InboxStore


@pytest.fixture
def store():
    return InboxStore(fakeredis.FakeRedis(decode_responses=True))


def test_existing_mail_returns_without_claiming_it(store):
    from scripts.inbox.receive import receive

    item = store.send("sender", "master", "review work")
    assert receive(store, "master", 1) == [item]
    assert store.get(item.id).state == "pending"


def test_late_seat_mail_wakes_the_waiting_master(store, monkeypatch):
    from scripts.inbox.receive import receive

    store.seats.occupy("master@sw", "master", at=1)
    subscribed = threading.Event()
    subscription = store.redis.pubsub()
    original = subscription.subscribe

    def subscribe(*args):
        original(*args)
        subscribed.set()

    monkeypatch.setattr(subscription, "subscribe", subscribe)
    monkeypatch.setattr(store.redis, "pubsub", lambda: subscription)
    with ThreadPoolExecutor() as pool:
        waiting = pool.submit(receive, store, "master", 2)
        assert subscribed.wait(1)
        assert not waiting.done()
        store.send("sender", "another", "ignore me")
        item = store.send("sender", "master@sw", "please read this")
        assert waiting.result(timeout=2) == [item]
    assert subscription.connection is None


def test_timeout_returns_no_mail_and_closes_subscription(store, monkeypatch):
    from scripts.inbox import receive

    subscription = store.redis.pubsub()
    monkeypatch.setattr(store.redis, "pubsub", lambda: subscription)
    times = iter([10, 10.5, 11])
    monkeypatch.setattr(receive, "monotonic", lambda: next(times))
    blocked = []

    def get_message(timeout):
        assert timeout == 0.5
        blocked.append(timeout)

    monkeypatch.setattr(subscription, "get_message", get_message)
    assert receive.receive(store, "master", 1) == []
    assert blocked == [0.5]
    assert subscription.connection is None


def test_mail_arriving_during_subscribe_is_not_missed(store, monkeypatch):
    from scripts.inbox.receive import receive

    subscription = store.redis.pubsub()
    original = subscription.subscribe
    items = []

    def subscribe(*args):
        original(*args)
        items.append(store.send("sender", "master", "racing arrival"))

    monkeypatch.setattr(subscription, "subscribe", subscribe)
    monkeypatch.setattr(store.redis, "pubsub", lambda: subscription)
    assert receive(store, "master", 1) == items


def test_failed_subscription_is_closed(store, monkeypatch):
    from scripts.inbox.receive import receive

    subscription = store.redis.pubsub()
    closed = []

    def subscribe(*args):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(subscription, "subscribe", subscribe)
    monkeypatch.setattr(subscription, "close", lambda: closed.append(True))
    monkeypatch.setattr(store.redis, "pubsub", lambda: subscription)
    with pytest.raises(RuntimeError, match="store unavailable"):
        receive(store, "master", 1)
    assert closed == [True]


@pytest.mark.parametrize("remaining", [0, -1])
def test_an_expired_wait_returns_an_empty_list_without_blocking(store, monkeypatch, remaining):
    from scripts.inbox import receive

    times = iter([0, 1 - remaining])
    monkeypatch.setattr(receive, "monotonic", lambda: next(times))
    subscription = store.redis.pubsub()
    monkeypatch.setattr(store.redis, "pubsub", lambda: subscription)
    blocked = []
    monkeypatch.setattr(subscription, "get_message", lambda **kw: blocked.append(kw))
    assert receive.receive(store, "master", 1) == []
    assert blocked == []
