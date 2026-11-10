import json
from typing import TYPE_CHECKING, Any

import pytest

from scripts.events import scheduler_state
from scripts.events.bus import StateConflict

if TYPE_CHECKING:
    from scripts.events.redis_state import RedisStateStore

pytestmark = pytest.mark.feature("scheduler-bounded-history")

KEY = "headed.entries"


def timer(name: str, state: str = "fired", owner: str = "process", due: float = 1) -> dict[str, Any]:
    return {"id": name, "kind": "timer", "owner": owner, "state": state, "due": due, "payload": name}


def history(count: int) -> dict[str, Any]:
    entries = {str(i): timer(str(i)) for i in range(count)}
    entries.update(
        {
            "active": timer("active", "active"),
            "pending": timer("pending", "pending"),
            "operator": timer("operator", owner="operator"),
            "recent": timer("recent", due=9_000),
            "retired": timer("retired", "cancelled", "operator"),
        }
    )
    return {"entries": entries, "retired": {"retired": True}, "lease": {"token": "holder", "expires": 20_000}}


def store() -> "RedisStateStore":
    import fakeredis

    from scripts.events.redis_state import RedisStateStore

    return RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler", prefix="proof")


def stored(state: "RedisStateStore") -> dict[str, Any]:
    entry = state.get(KEY)
    assert entry is not None
    return json.loads(entry.value)


def revision(state: "RedisStateStore") -> int:
    entry = scheduler_state.read(state, KEY, "headed:entries")
    assert entry is not None
    return entry.revision


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_state.time, "time", lambda: 10_000.0)


@pytest.mark.feature("scheduler-bounded-history")
def test_live_keeps_live_and_operator_timers_and_drops_retired_and_abandoned_ones() -> None:
    assert scheduler_state.live(history(3), 10_000) == {
        "entries": {
            "active": timer("active", "active"),
            "pending": timer("pending", "pending"),
            "operator": timer("operator", owner="operator"),
            "recent": timer("recent", due=9_000),
        },
        "retired": {},
        "lease": {"token": "holder", "expires": 20_000},
    }


@pytest.mark.feature("scheduler-bounded-history")
def test_live_reads_a_document_missing_its_entries_or_retired_map() -> None:
    assert scheduler_state.live({"entries": {"a": timer("a", "active")}}, 10_000) == {
        "entries": {"a": timer("a", "active")},
        "retired": {},
    }
    assert scheduler_state.live({"retired": {"a": True}}, 10_000) == {"entries": {}, "retired": {}}


@pytest.mark.feature("scheduler-bounded-history")
def test_an_oversized_document_is_rewritten_once_to_its_live_timers(
    monkeypatch: pytest.MonkeyPatch, clock: None
) -> None:
    state = store()
    state.create(KEY, json.dumps(history(200)).encode())
    monkeypatch.setattr(scheduler_state, "MAX_DOCUMENT", 1_024)
    entry = scheduler_state.read(state, KEY, "headed:entries")
    assert entry is not None and entry.revision == 2
    assert json.loads(entry.value) == stored(state)
    assert set(stored(state)["entries"]) == {"active", "pending", "operator", "recent"}
    assert stored(state)["retired"] == {}
    assert stored(state)["lease"] == {"token": "holder", "expires": 20_000}
    assert revision(state) == 2


@pytest.mark.feature("scheduler-bounded-history")
def test_a_document_at_the_size_limit_is_left_alone(monkeypatch: pytest.MonkeyPatch, clock: None) -> None:
    state = store()
    state.create(KEY, json.dumps(history(20)).encode())
    size = state.redis.hstrlen(state.watch_key, KEY)
    shrink = scheduler_state.shrink
    shrunk: list[str] = []

    def spy(target: "RedisStateStore", key: str) -> None:
        shrunk.append(key)
        shrink(target, key)

    monkeypatch.setattr(scheduler_state, "shrink", spy)
    monkeypatch.setattr(scheduler_state, "MAX_DOCUMENT", size)
    assert revision(state) == 1
    assert shrunk == []
    shrink(state, KEY)
    assert revision(state) == 1
    monkeypatch.setattr(scheduler_state, "MAX_DOCUMENT", size - 1)
    assert revision(state) == 2
    assert shrunk == [KEY]


@pytest.mark.feature("scheduler-bounded-history")
def test_shrink_skips_a_missing_document(monkeypatch: pytest.MonkeyPatch) -> None:
    state = store()
    monkeypatch.setattr(scheduler_state, "MAX_DOCUMENT", -1)
    scheduler_state.shrink(state, KEY)
    assert state.get(KEY) is None


@pytest.mark.feature("scheduler-bounded-history")
def test_shrink_retries_a_concurrent_write_and_keeps_its_live_timer(
    monkeypatch: pytest.MonkeyPatch, clock: None
) -> None:
    state = store()
    state.create(KEY, json.dumps(history(50)).encode())
    monkeypatch.setattr(scheduler_state, "MAX_DOCUMENT", 1_024)
    original = state.update
    raced: list[int] = []

    def racing(key: str, value: bytes, revision: int) -> int:
        if not raced:
            document = history(50)
            document["entries"]["new"] = timer("new", "active")
            raced.append(original(key, json.dumps(document).encode(), revision))
        return original(key, value, revision)

    monkeypatch.setattr(state, "update", racing)
    scheduler_state.shrink(state, KEY)
    assert raced == [2]
    monkeypatch.setattr(scheduler_state, "MAX_DOCUMENT", 1 << 30)
    assert revision(state) == 3
    assert set(stored(state)["entries"]) == {"active", "pending", "operator", "recent", "new"}


@pytest.mark.feature("scheduler-bounded-history")
def test_shrink_gives_up_on_a_document_that_keeps_changing(monkeypatch: pytest.MonkeyPatch, clock: None) -> None:
    state = store()
    state.create(KEY, json.dumps(history(50)).encode())
    monkeypatch.setattr(scheduler_state, "MAX_DOCUMENT", 1_024)
    calls: list[int] = []

    def conflict(key: str, value: bytes, revision: int) -> int:
        calls.append(revision)
        raise StateConflict("changed")

    monkeypatch.setattr(state, "update", conflict)
    with pytest.raises(StateConflict) as raised:
        scheduler_state.shrink(state, KEY)
    assert str(raised.value) == "scheduler recovery 'headed.entries' kept changing"
    assert len(calls) == 16


@pytest.mark.feature("scheduler-bounded-history")
def test_legacy_migration_skips_abandoned_process_timers(clock: None) -> None:
    state = store()
    legacy = history(100)["entries"]
    state.redis.hset("headed:entries", mapping={name: json.dumps(row) for name, row in legacy.items()})
    state.redis.hset("headed:retired", mapping={"retired": "1"})
    entry = scheduler_state.read(state, KEY, "headed:entries")
    assert entry is not None and json.loads(entry.value) == {
        "entries": {name: legacy[name] for name in ("active", "pending", "operator", "recent", "retired")},
        "retired": {"retired": True},
    }
