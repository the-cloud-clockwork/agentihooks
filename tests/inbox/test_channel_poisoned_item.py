"""One inbox item that always fails to show is requeued alone while the rest of its push batch reaches the session."""

from typing import Any

import pytest

from scripts.inbox import channel, seen
from scripts.inbox.store import InboxStore, Item

pytestmark = [pytest.mark.xdist_group("mcp-sdk"), pytest.mark.usefixtures("mcp_sdk")]


class Session:
    """The session side of the push stream, recording each item id it is shown."""

    def __init__(self) -> None:
        self.shown: list[str] = []

    async def send(self, message: Any) -> None:
        self.shown.append(message.message.root.params["meta"]["item_id"])


def _three(inbox: InboxStore) -> list[Item]:
    return [inbox.send("alice", "bob", f"item {n}", ref=f"sw:{n}:c{n}") for n in range(3)]


def _requeued(inbox: InboxStore, item_id: str) -> tuple[str, str, str]:
    last = [entry for entry in inbox.history(item_id) if "state" in entry][-1]
    return last["state"], last["by"], last["reason"]


def _pass(inbox: InboxStore, session: Session) -> None:
    import anyio

    anyio.run(channel._deliver, inbox, "bob", session, channel.Pushed())  # pyright: ignore[reportPrivateUsage]


@pytest.mark.feature("inbox-push-poisoned-item")
def test_an_item_whose_seen_check_always_fails_is_requeued_alone_and_the_other_two_are_pushed(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, inbox = channel_backend
    first, poisoned, last = _three(inbox)
    checked = seen._shown_elsewhere  # pyright: ignore[reportPrivateUsage]

    def failing(marks: Any, store: InboxStore, item: Item, me: str) -> bool:
        if item.id == poisoned.id:
            raise ValueError("unreadable history entry")
        return checked(marks, store, item, me)

    monkeypatch.setattr(seen, "_shown_elsewhere", failing)
    session = Session()
    _pass(inbox, session)
    assert session.shown == [first.id, last.id]
    assert inbox.get(poisoned.id).state == "pending"
    assert _requeued(inbox, poisoned.id) == ("pending", "bob", seen.UNCHECKED)
    assert {inbox.get(first.id).state, inbox.get(last.id).state} == {"delivered"}


@pytest.mark.feature("inbox-push-poisoned-item")
def test_an_item_whose_notification_always_fails_to_build_is_requeued_alone_and_the_other_two_are_pushed(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, inbox = channel_backend
    first, poisoned, last = _three(inbox)
    built = channel.event

    def failing(item: Item) -> dict[str, Any]:
        if item.id == poisoned.id:
            raise TypeError("text is not a string")
        return built(item)

    monkeypatch.setattr(channel, "event", failing)
    session = Session()
    _pass(inbox, session)
    assert session.shown == [first.id, last.id]
    assert inbox.get(poisoned.id).state == "pending"
    assert _requeued(inbox, poisoned.id) == ("pending", "bob", channel.UNSHOWN)
    assert {inbox.get(first.id).state, inbox.get(last.id).state} == {"delivered"}


@pytest.mark.feature("inbox-push-poisoned-item")
def test_a_full_batch_of_poisoned_items_is_never_claimed_again_and_the_item_behind_it_is_pushed(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    import anyio

    _, inbox = channel_backend
    poisoned = [inbox.send("alice", "bob", f"poisoned {n}", ref=f"sw:{n}:p{n}") for n in range(seen.CLAIM_BATCH)]
    healthy = inbox.send("alice", "bob", "healthy", ref="sw:9:h9")
    attempts: list[str] = []
    checked = seen._shown_elsewhere  # pyright: ignore[reportPrivateUsage]

    def failing(marks: Any, store: InboxStore, item: Item, me: str) -> bool:
        if item.id != healthy.id:
            attempts.append(item.id)
            raise ValueError("unreadable history entry")
        return checked(marks, store, item, me)

    monkeypatch.setattr(seen, "_shown_elsewhere", failing)
    session, pushed = Session(), channel.Pushed()
    for _ in range(3):
        anyio.run(channel._deliver, inbox, "bob", session, pushed)  # pyright: ignore[reportPrivateUsage]
    assert session.shown == [healthy.id]
    assert sorted(attempts) == sorted(item.id for item in poisoned)
    assert {inbox.get(item.id).state for item in poisoned} == {"pending"}


@pytest.mark.feature("inbox-push-poisoned-item")
def test_a_redis_error_in_the_seen_check_leaves_the_claim_for_the_push_loop_backoff(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import RedisError

    _, inbox = channel_backend
    item = inbox.send("alice", "bob", "during an outage", ref="sw:1:c1")

    def failing(marks: Any, store: InboxStore, item: Item, me: str) -> bool:
        raise RedisError("connection lost")

    monkeypatch.setattr(seen, "_shown_elsewhere", failing)
    skip: set[str] = set()
    with pytest.raises(RedisError):
        seen.claim(inbox, "bob", skip=skip)
    assert skip == set()
    assert inbox.get(item.id).state == "delivered"
