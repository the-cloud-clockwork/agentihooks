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


@pytest.mark.feature("inbox-claim-redis-error-requeue")
def test_a_redis_error_mid_batch_returns_every_item_the_claim_delivered_to_pending_and_a_healthy_pass_pushes_them(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import RedisError

    _, inbox = channel_backend
    first, broken, last = _three(inbox)
    checked = seen._shown_elsewhere  # pyright: ignore[reportPrivateUsage]

    def failing(marks: Any, store: InboxStore, item: Item, me: str) -> bool:
        if item.id == broken.id:
            raise RedisError("connection lost")
        return checked(marks, store, item, me)

    monkeypatch.setattr(seen, "_shown_elsewhere", failing)
    session = Session()
    with pytest.raises(RedisError):
        _pass(inbox, session)
    assert session.shown == []
    assert [inbox.get(item.id).state for item in (first, broken, last)] == ["pending", "pending", "pending"]
    assert _requeued(inbox, first.id) == ("pending", "bob", seen.CLAIM_FAILED)
    assert _requeued(inbox, broken.id) == ("pending", "bob", seen.CLAIM_FAILED)
    monkeypatch.setattr(seen, "_shown_elsewhere", checked)
    _pass(inbox, session)
    assert session.shown == [first.id, broken.id, last.id]


@pytest.mark.feature("inbox-claim-redis-error-requeue")
def test_a_failed_return_keeps_the_original_redis_error_and_still_returns_the_rest_of_the_batch(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import RedisError

    _, inbox = channel_backend
    first, broken, last = _three(inbox)
    checked = seen._shown_elsewhere  # pyright: ignore[reportPrivateUsage]
    requeue = inbox.requeue
    lost = RedisError("connection lost")

    def failing(marks: Any, store: InboxStore, item: Item, me: str) -> bool:
        if item.id == broken.id:
            raise lost
        return checked(marks, store, item, me)

    def flaky_requeue(item_id: str, by: str, reason: str, before: int | None = None) -> Item | None:
        if item_id == first.id:
            raise RedisError("requeue lost")
        return requeue(item_id, by, reason, before)

    monkeypatch.setattr(seen, "_shown_elsewhere", failing)
    monkeypatch.setattr(inbox, "requeue", flaky_requeue)
    with pytest.raises(RedisError) as raised:
        seen.claim(inbox, "bob")
    assert raised.value is lost
    assert [inbox.get(item.id).state for item in (first, broken, last)] == ["delivered", "pending", "pending"]
    assert _requeued(inbox, broken.id) == ("pending", "bob", seen.CLAIM_FAILED)


@pytest.mark.feature("inbox-claim-redis-error-requeue")
def test_a_redis_error_delivering_the_second_item_returns_the_first_to_pending(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import RedisError

    _, inbox = channel_backend
    first, broken, last = _three(inbox)
    deliver = inbox.deliver

    def flaky_deliver(item_id: str, receiver: str) -> Item | None:
        if item_id == broken.id:
            raise RedisError("connection lost")
        return deliver(item_id, receiver)

    monkeypatch.setattr(inbox, "deliver", flaky_deliver)
    with pytest.raises(RedisError):
        seen.claim(inbox, "bob")
    assert [inbox.get(item.id).state for item in (first, broken, last)] == ["pending", "pending", "pending"]
    assert _requeued(inbox, first.id) == ("pending", "bob", seen.CLAIM_FAILED)


def _clocked_passes(inbox: InboxStore, monkeypatch: pytest.MonkeyPatch) -> tuple[Session, list[int], Any]:
    import anyio

    clock = [seen.now_ms()]
    monkeypatch.setattr(seen, "now_ms", lambda: clock[0])
    session, pushed = Session(), channel.Pushed()

    def deliver() -> None:
        anyio.run(channel._deliver, inbox, "bob", session, pushed)  # pyright: ignore[reportPrivateUsage]

    return session, clock, deliver


@pytest.mark.feature("inbox-poisoned-id-expiry")
def test_an_id_poisoned_by_one_seen_check_failure_is_pushed_again_after_one_redelivery_window(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, inbox = channel_backend
    item = inbox.send("alice", "bob", "once flaky", ref="sw:1:f1")
    checked = seen._shown_elsewhere  # pyright: ignore[reportPrivateUsage]
    failures = [ValueError("transient")]

    def flaky(marks: Any, store: InboxStore, claimed: Item, me: str) -> bool:
        if failures:
            raise failures.pop()
        return checked(marks, store, claimed, me)

    monkeypatch.setattr(seen, "_shown_elsewhere", flaky)
    session, clock, deliver = _clocked_passes(inbox, monkeypatch)
    deliver()
    clock[0] += seen.redelivery_ms() - 1
    deliver()
    assert session.shown == []
    assert inbox.get(item.id).state == "pending"
    clock[0] += 1
    deliver()
    assert session.shown == [item.id]


@pytest.mark.feature("inbox-poisoned-id-expiry")
def test_an_id_poisoned_by_one_notification_failure_is_pushed_again_after_one_redelivery_window(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, inbox = channel_backend
    item = inbox.send("alice", "bob", "once unbuildable", ref="sw:1:u1")
    built = channel.event
    failures = [TypeError("transient")]

    def flaky(claimed: Item) -> dict[str, Any]:
        if failures:
            raise failures.pop()
        return built(claimed)

    monkeypatch.setattr(channel, "event", flaky)
    session, clock, deliver = _clocked_passes(inbox, monkeypatch)
    deliver()
    clock[0] += seen.redelivery_ms() - 1
    deliver()
    assert session.shown == []
    clock[0] += 1
    deliver()
    assert session.shown == [item.id]
