"""One inbox item that always fails to show is requeued alone while the rest of its push batch reaches the session."""

from typing import Any, NoReturn

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
    import anyio

    _, inbox = channel_backend
    first, poisoned, last = _three(inbox)
    built = channel.event
    attempts: list[str] = []

    def failing(item: Item) -> dict[str, Any]:
        if item.id == poisoned.id:
            attempts.append(item.id)
            raise TypeError("text is not a string")
        return built(item)

    monkeypatch.setattr(channel, "event", failing)
    session, pushed = Session(), channel.Pushed()
    for _ in range(2):
        anyio.run(channel._deliver, inbox, "bob", session, pushed)  # pyright: ignore[reportPrivateUsage]
    assert attempts == [poisoned.id]
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


def _failing_check(broken: Item, error: Exception) -> Any:
    checked = seen._shown_elsewhere  # pyright: ignore[reportPrivateUsage]

    def failing(marks: Any, store: InboxStore, item: Item, me: str) -> bool:
        if item.id == broken.id:
            raise error
        return checked(marks, store, item, me)

    return failing


def _states(inbox: InboxStore, items: list[Item]) -> list[str]:
    return [inbox.get(item.id).state for item in items]


@pytest.mark.feature("inbox-claim-redis-error-requeue")
def test_a_redis_error_mid_batch_pushes_the_checked_item_and_returns_the_rest_to_pending(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import RedisError

    _, inbox = channel_backend
    first, broken, last = _three(inbox)
    checked = seen._shown_elsewhere  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(seen, "_shown_elsewhere", _failing_check(broken, RedisError("connection lost")))
    session = Session()
    _pass(inbox, session)
    assert session.shown == [first.id]
    assert _states(inbox, [first, broken, last]) == ["delivered", "pending", "pending"]
    assert _requeued(inbox, broken.id) == ("pending", "bob", seen.CLAIM_FAILED)
    monkeypatch.setattr(seen, "_shown_elsewhere", checked)
    _pass(inbox, session)
    assert session.shown == [first.id, broken.id, last.id]


@pytest.mark.feature("inbox-claim-redis-error-requeue")
def test_a_redis_error_delivering_the_second_item_still_returns_the_first_for_showing(
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
    assert [item.id for item in seen.claim(inbox, "bob")] == [first.id]
    assert _states(inbox, [first, broken, last]) == ["delivered", "pending", "pending"]


def _close_fails(
    inbox: InboxStore, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> tuple[list[str], list[str], tuple[str, str, str]]:
    first, broken, last = _three(inbox)
    checked = seen._shown_elsewhere  # pyright: ignore[reportPrivateUsage]

    def elsewhere(marks: Any, store: InboxStore, item: Item, me: str) -> bool:
        return item.id == broken.id or checked(marks, store, item, me)

    def failing_close(item_id: str, by: str, kind: str, detail: str = "") -> NoReturn:
        raise error

    monkeypatch.setattr(seen, "_shown_elsewhere", elsewhere)
    monkeypatch.setattr(inbox, "close", failing_close)
    claimed = [item.id for item in seen.claim(inbox, "bob")]
    assert claimed == [first.id]
    return claimed, _states(inbox, [first, broken, last]), _requeued(inbox, broken.id)


@pytest.mark.feature("inbox-claim-redis-error-requeue")
def test_a_redis_error_closing_an_item_shown_elsewhere_returns_it_to_pending_and_shows_the_first(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import RedisError

    _, inbox = channel_backend
    _, states, requeued = _close_fails(inbox, monkeypatch, RedisError("connection lost"))
    assert states == ["delivered", "pending", "pending"]
    assert requeued == ("pending", "bob", seen.CLAIM_FAILED)


@pytest.mark.feature("inbox-claim-redis-error-requeue")
def test_a_store_error_closing_an_item_shown_elsewhere_returns_it_to_pending_and_shows_the_first(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.inbox.store import InboxError

    _, inbox = channel_backend
    _, states, requeued = _close_fails(inbox, monkeypatch, InboxError("message changed meanwhile"))
    assert states == ["delivered", "pending", "pending"]
    assert requeued == ("pending", "bob", seen.CLAIM_FAILED)


@pytest.mark.feature("inbox-claim-redis-error-requeue")
def test_a_redis_error_returning_an_unsettled_item_returns_it_to_pending_and_shows_the_first(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import RedisError

    from scripts.inbox.store import InboxError

    _, inbox = channel_backend
    first, broken, last = _three(inbox)
    requeue = inbox.requeue

    def flaky_requeue(item_id: str, by: str, reason: str, before: int | None = None) -> Item | None:
        if reason == seen.UNSETTLED:
            raise RedisError("connection lost")
        return requeue(item_id, by, reason, before)

    monkeypatch.setattr(seen, "_shown_elsewhere", _failing_check(broken, InboxError("owner changed meanwhile")))
    monkeypatch.setattr(inbox, "requeue", flaky_requeue)
    assert [item.id for item in seen.claim(inbox, "bob")] == [first.id]
    assert _states(inbox, [first, broken, last]) == ["delivered", "pending", "pending"]
    assert _requeued(inbox, broken.id) == ("pending", "bob", seen.CLAIM_FAILED)


def _return_fails(inbox: InboxStore, monkeypatch: pytest.MonkeyPatch, error: Exception) -> list[str]:
    from redis.exceptions import RedisError

    first, broken, last = _three(inbox)

    def failing_requeue(item_id: str, by: str, reason: str, before: int | None = None) -> NoReturn:
        raise error

    monkeypatch.setattr(seen, "_shown_elsewhere", _failing_check(broken, RedisError("connection lost")))
    monkeypatch.setattr(inbox, "requeue", failing_requeue)
    assert [item.id for item in seen.claim(inbox, "bob")] == [first.id]
    return _states(inbox, [first, broken, last])


@pytest.mark.feature("inbox-claim-redis-error-requeue")
def test_an_in_flight_item_whose_return_hits_redis_too_stays_delivered_and_the_first_is_still_shown(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import RedisError

    _, inbox = channel_backend
    assert _return_fails(inbox, monkeypatch, RedisError("still down")) == ["delivered", "delivered", "pending"]


@pytest.mark.feature("inbox-claim-redis-error-requeue")
def test_an_in_flight_item_whose_return_is_refused_as_changed_stays_delivered_and_the_first_is_still_shown(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.inbox.store import InboxError

    _, inbox = channel_backend
    assert _return_fails(inbox, monkeypatch, InboxError("message changed meanwhile")) == [
        "delivered",
        "delivered",
        "pending",
    ]


def _clocked_passes(inbox: InboxStore, monkeypatch: pytest.MonkeyPatch) -> tuple[Session, list[int], Any]:
    import anyio

    from scripts.inbox import store as store_module

    clock = [seen.now_ms()]
    monkeypatch.setattr(seen, "now_ms", lambda: clock[0])
    monkeypatch.setattr(store_module, "now_ms", lambda: clock[0])
    session, pushed = Session(), channel.Pushed()

    def deliver() -> None:
        anyio.run(channel._deliver, inbox, "bob", session, pushed)  # pyright: ignore[reportPrivateUsage]

    return session, clock, deliver


def _hold_ms() -> int:
    from scripts.inbox import wake

    return seen.redelivery_ms() + wake.window_ms({})


@pytest.mark.feature("inbox-poisoned-id-expiry")
def test_an_id_poisoned_by_one_seen_check_failure_is_pushed_again_once_its_hold_passes(
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
    clock[0] += _hold_ms() - 1
    deliver()
    assert session.shown == []
    assert inbox.get(item.id).state == "pending"
    clock[0] += 1
    deliver()
    assert session.shown == [item.id]


@pytest.mark.feature("inbox-poisoned-id-expiry")
def test_an_id_poisoned_by_one_notification_failure_is_pushed_again_once_its_hold_passes(
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
    clock[0] += _hold_ms() - 1
    deliver()
    assert session.shown == []
    clock[0] += 1
    deliver()
    assert session.shown == [item.id]


@pytest.mark.feature("inbox-poisoned-id-expiry")
def test_an_always_failing_item_to_an_idle_channel_session_is_escalated_to_the_master_at_default_windows(
    channel_backend: tuple[Any, InboxStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.inbox import wake

    _, inbox = channel_backend
    item = inbox.send("alice", "bob", "always unreadable", ref="sw:1:a1")
    monkeypatch.setattr(seen, "_shown_elsewhere", _failing_check(item, ValueError("unreadable history entry")))
    monkeypatch.setattr(wake.outage, "paused_ms", lambda slug, since, now: 0)
    session, clock, deliver = _clocked_passes(inbox, monkeypatch)
    decisions: list[str | None] = []
    for _ in range(10):
        deliver()
        current = inbox.get(item.id)
        decisions.append(wake.decide(current, None, inbox.history(item.id), clock[0], wake.window_ms({}), "sw"))
        clock[0] += 60_000
    assert session.shown == []
    assert decisions[:6] == [None, None, None, None, None, wake.TO_MASTER]
