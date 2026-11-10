"""Inbox reads and seen marks on the bus state backend retry when an alias moves in the names store meanwhile."""

import itertools
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.inbox import seen
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import name_state

pytestmark = pytest.mark.unit


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    opened = memory.open_bus(BusConfig())
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setattr(name_state, "_OPENED", {})
    monkeypatch.setattr(name_state.state, "open_state", opened.state)
    yield opened
    opened.close()


@pytest.fixture
def store(bus: Any) -> InboxStore:
    import fakeredis

    return InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))


def _point(store: InboxStore, old: str, new: str) -> None:
    store.names.names_state.amend("alias", old, lambda _: new, lambda _: None)


def _moving(store: InboxStore, old: str, targets: Iterator[str], real: Callable[..., Any]) -> Callable[..., Any]:
    def moved(*args: Any, **kwargs: Any) -> Any:
        _point(store, old, next(targets))
        return real(*args, **kwargs)

    return moved


@pytest.mark.parametrize("read", ["mailbox", "pending_mail"])
@pytest.mark.feature("inbox-alias-store-revision")
def test_a_mailbox_read_retries_when_the_alias_moves_on_the_bus_meanwhile(
    store: InboxStore, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    item = store.send("alice", "engineer@abcdef-0002", "review my branch")
    real = store.seats.seat_of
    calls = []

    def seat_of(*args: Any, **kwargs: Any) -> Any:
        if not calls:
            _point(store, "sw-eng-1", "engineer@abcdef-0002")
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(store.seats, "seat_of", seat_of)
    assert [listed.id for listed in getattr(store, read)("sw-eng-1")] == [item.id]
    assert [call[0] for call in calls] == ["sw-eng-1", "engineer@abcdef-0002"]


@pytest.mark.feature("inbox-alias-store-revision")
def test_a_mailbox_read_retries_when_an_alias_joins_its_owner_on_the_bus_meanwhile(
    store: InboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = store.send("alice", "sw-eng-1", "review my branch")
    real = store.seats.seat_of
    calls = []

    def seat_of(*args: Any, **kwargs: Any) -> Any:
        if not calls:
            store.names.names_state.amend("aliases-of", "engineer@abcdef-0002", lambda _: ["sw-eng-1"], lambda _: None)
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(store.seats, "seat_of", seat_of)
    assert [listed.id for listed in store.mailbox("engineer@abcdef-0002")] == [item.id]
    assert [call[0] for call in calls] == ["engineer@abcdef-0002", "engineer@abcdef-0002"]


@pytest.mark.feature("inbox-alias-store-revision")
def test_a_mailbox_read_refuses_when_the_alias_never_settles_on_the_bus(
    store: InboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = (f"engineer@abcdef-{n:04d}" for n in itertools.count(1))
    monkeypatch.setattr(store.seats, "seat_of", _moving(store, "sw-eng-1", targets, store.seats.seat_of))
    with pytest.raises(InboxError) as refused:
        store.mailbox("sw-eng-1")
    assert str(refused.value) == "aliases for sw-eng-1 changed meanwhile"


@pytest.mark.feature("inbox-alias-store-revision")
def test_a_seen_mark_retries_when_the_alias_moves_on_the_bus_meanwhile(
    store: InboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    marks = seen.SeenMarks(store.redis)
    real = seen.owner_key
    calls = []

    def owner_key(name: str) -> str:
        if not calls:
            _point(store, "sw-eng-1", "engineer@abcdef-0002")
        calls.append(name)
        return real(name)

    monkeypatch.setattr(seen, "owner_key", owner_key)
    assert marks.mark("sw-eng-1", "sw:7:note") is True
    assert calls == ["sw-eng-1", "sw-eng-1", "engineer@abcdef-0002", "engineer@abcdef-0002"]
    assert marks.seen("engineer@abcdef-0002", "sw:7:note")
    assert not store.redis.sismember(seen.SeenMarks.key("sw-eng-1"), "sw:7:note")


@pytest.mark.feature("inbox-alias-store-revision")
def test_a_seen_mark_retries_when_the_alias_moves_on_the_bus_during_its_write(
    store: InboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    marks = seen.SeenMarks(store.redis)
    real = seen.SeenMarks.key
    calls = []

    def key(name: str) -> str:
        if not calls:
            _point(store, "sw-eng-1", "engineer@abcdef-0002")
        calls.append(name)
        return real(name)

    monkeypatch.setattr(seen.SeenMarks, "key", staticmethod(key))
    assert marks.mark("sw-eng-1", "sw:7:note") is True
    assert calls == ["sw-eng-1", "sw-eng-1", "sw-eng-1", "engineer@abcdef-0002", "engineer@abcdef-0002"]
    assert marks.seen("engineer@abcdef-0002", "sw:7:note")
    assert not store.redis.sismember(seen.SeenMarks.key("sw-eng-1"), "sw:7:note")


@pytest.mark.feature("inbox-alias-store-revision")
def test_a_seen_mark_refuses_when_the_alias_never_settles_on_the_bus(
    store: InboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = (f"engineer@abcdef-{n:04d}" for n in itertools.count(1))
    monkeypatch.setattr(seen, "owner_key", _moving(store, "sw-eng-1", targets, seen.owner_key))
    with pytest.raises(InboxError) as refused:
        seen.SeenMarks(store.redis).mark("sw-eng-1", "sw:7:note")
    assert str(refused.value) == "the delivery owner of sw-eng-1 changed meanwhile; run it again"


@pytest.mark.feature("inbox-alias-store-revision")
def test_the_redis_backend_reads_no_store_revisions_because_its_watch_sees_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    names = name_state.NameState(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    names.amend("alias", "sw-eng-1", lambda _: "engineer@abcdef-0002", lambda _: None)
    assert names.revisions(("alias", "sw-eng-1"), ("aliases-of", "engineer@abcdef-0002")) == ()
