import json

import pytest

from scripts.events import factory, memory, state
from scripts.events.bus import StateConflict
from scripts.events.config import BusConfig
from scripts.swarm import fencing, lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def clock(monkeypatch):
    at = [1000]
    monkeypatch.setattr(fencing, "now_ms", lambda: at[0])
    return at


@pytest.fixture
def bus(monkeypatch):
    opened = memory.open_bus(BusConfig())
    monkeypatch.setattr(factory, "open_bus", lambda config: opened)
    monkeypatch.setenv(state.BACKEND_ENV, "bus")
    return opened


def _saved(states, slug):
    return json.loads(states.get(slug).value)


def test_a_new_swarm_reads_an_empty_fencing_document(store):
    doc = fencing.read(store, "sw")
    assert doc["lease"] is None
    assert doc["epoch"] == 0
    assert doc["executions"] == {}


def test_a_first_read_returns_the_document_created_by_a_competitor(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.events.redis_state import RedisStateStore

    monkeypatch.setenv(state.BACKEND_ENV, "redis")
    original_create = RedisStateStore.create
    winner = {"epoch": 7, "legacy_owner": "competitor"}

    def competing_create(self: RedisStateStore, key: str, value: bytes) -> int:
        original_create(self, key, json.dumps(winner).encode())
        return original_create(self, key, value)

    monkeypatch.setattr(RedisStateStore, "create", competing_create)

    doc = fencing.read(store, "sw")

    assert doc["epoch"] == 7
    assert doc["legacy_owner"] == "competitor"
    assert fencing.read(store, "sw") == doc


def test_a_write_commits_the_change_and_returns_its_result(store):
    def change(doc):
        doc["epoch"] = 4
        return "done"

    assert fencing.write(store, "sw", change) == "done"
    assert fencing.read(store, "sw")["epoch"] == 4
    assert fencing.read(store, "other")["epoch"] == 0


def test_a_write_that_raises_leaves_the_document_unchanged(store):
    fencing.write(store, "sw", lambda doc: doc.update(epoch=3))

    def refused(doc):
        doc["epoch"] = 9
        raise SwarmError("refused")

    with pytest.raises(SwarmError, match="refused"):
        fencing.write(store, "sw", refused)
    assert fencing.read(store, "sw")["epoch"] == 3


def test_a_write_retries_on_a_competing_revision_and_sees_the_competitor(store, monkeypatch):
    fencing.write(store, "sw", lambda doc: doc.update(epoch=1))
    states = state.for_store(store, fencing.BUCKET)
    raced, seen = [], []

    def change(doc):
        seen.append(doc["epoch"])
        if not raced:
            raced.append(True)
            entry = states.get("sw")
            states.update("sw", json.dumps({**json.loads(entry.value), "epoch": 5}).encode(), entry.revision)
        doc["epoch"] += 1

    monkeypatch.setattr(fencing.state, "for_store", lambda *args, **kwargs: states)
    fencing.write(store, "sw", change)
    assert seen == [1, 5]
    assert fencing.read(store, "sw")["epoch"] == 6


def test_a_write_that_keeps_conflicting_is_refused(store, monkeypatch):
    class Racing:
        def __init__(self, inner):
            self.inner = inner

        def get(self, key):
            return self.inner.get(key)

        def create(self, key, value):
            return self.inner.create(key, value)

        def update(self, key, value, revision):
            raise StateConflict("raced")

    states = Racing(state.for_store(store, fencing.BUCKET))
    monkeypatch.setattr(fencing.state, "for_store", lambda *args, **kwargs: states)
    with pytest.raises(SwarmError, match="the swarm fencing state kept changing"):
        fencing.write(store, "sw", lambda doc: doc.update(epoch=2))


def test_a_stale_lease_holder_cannot_write_through_the_fence(store, monkeypatch):
    at = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda saved: at[0])
    first = lease.acquire(store, "sw", "home")
    at[0] = first.expires_at
    second = lease.acquire(store, "sw", "other")
    with pytest.raises(SwarmError, match="the controller lease is stale"):
        lease.fenced(store, "sw", first, lambda doc: doc["intents"].update(stale={"epoch": first.epoch}))
    assert fencing.read(store, "sw")["intents"] == {}
    lease.fenced(store, "sw", second, lambda doc: doc["intents"].update(live={"epoch": second.epoch}))
    assert fencing.read(store, "sw")["intents"] == {"live": {"epoch": second.epoch}}


def test_a_fenced_write_is_refused_once_the_lease_expires(store, monkeypatch):
    at = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda saved: at[0])
    held = lease.acquire(store, "sw", "home")
    at[0] = held.expires_at
    with pytest.raises(SwarmError, match="the controller lease is stale"):
        lease.fenced(store, "sw", held, lambda doc: doc.update(orphans={"a": 1}))
    assert fencing.read(store, "sw")["orphans"] == {}


def test_the_lease_lives_on_the_bus_state_store_and_writes_no_redis_key(store, bus, clock):
    before = set(store.redis.keys("*"))
    held = lease.acquire(store, "sw", "home")
    assert set(store.redis.keys("*")) == before
    saved = _saved(bus.state(fencing.BUCKET), "sw")
    assert saved["lease"] == {"owner": "home", "epoch": 1, "expires_at": held.expires_at}
    assert saved["epoch"] == 1
    assert saved["leader_changes"] == 1
    assert lease.current(store, "sw") == held


def test_a_first_write_imports_the_legacy_redis_lease_once(store, clock):
    store.redis.set(store.key("sw", "control-owner"), json.dumps({"owner": "home", "epoch": 3, "expires_at": 9000}))
    store.redis.set(store.key("sw", "control-epoch"), 3)
    store.redis.set(store.key("sw", "controller-leader-changes"), 2)
    assert lease.current(store, "sw") == lease.Lease("home", 3, 9000)
    fencing.write(store, "sw", lambda doc: None)
    store.redis.delete(store.key("sw", "control-owner"))
    doc = fencing.read(store, "sw")
    assert (doc["lease"], doc["epoch"], doc["leader_changes"]) == (
        {"owner": "home", "epoch": 3, "expires_at": 9000},
        3,
        2,
    )


def test_a_first_read_imports_legacy_task_claims_journals_and_fences(store):
    store.redis.set(store.key("sw", "task-authority", "t1"), json.dumps({"holder": "eng-a"}))
    rows = [json.dumps({"event": event}) for event in "abc"]
    store.redis.rpush(store.key("sw", "claim-journal", "t1"), *rows)
    store.redis.set(store.key("sw", "heartbeat-sequence", "h1"), json.dumps({"sequence": 2}))
    store.redis.set(store.key("sw", "launch-grants-disabled"), "2026-10-29T00:00:00Z")
    store.redis.hset(store.key("sw", "controller-orphans"), "missing_pod", "2")

    doc = fencing.read(store, "sw")

    assert doc["claims"] == {"t1": {"holder": "eng-a"}}
    assert doc["journals"] == {"t1": [{"event": "a"}, {"event": "b"}, {"event": "c"}]}
    assert doc["fences"] == {store.key("sw", "heartbeat-sequence", "h1"): {"sequence": 2}}
    assert doc["grants_disabled"] == "2026-10-29T00:00:00Z"
    assert doc["orphans"] == {"missing_pod": 2}
    assert fencing.read(store, "fresh")["grants_disabled"] == ""


def test_replacing_without_a_saved_document_imports_the_legacy_keys(store):
    fencing.write(store, "sw", lambda doc: doc.update(epoch=9))
    store.redis.set(store.key("sw", "control-epoch"), 4)

    fencing.replace(store, "sw", None)

    assert fencing.read(store, "sw")["epoch"] == 4


def test_a_watched_transaction_aborts_when_the_fencing_document_changes(store):
    from redis.exceptions import WatchError

    fencing.write(store, "sw", lambda doc: doc.update(epoch=1))
    with store.redis.pipeline() as pipe:
        fencing.watch(store, pipe)
        fencing.write(store, "sw", lambda doc: doc.update(epoch=2))
        pipe.multi()
        pipe.set("probe", "1")
        with pytest.raises(WatchError):
            pipe.execute()
    assert store.redis.get("probe") is None


def test_the_document_is_stored_as_compact_json(store):
    fencing.write(store, "sw", lambda doc: doc.update(epoch=3))

    raw = state.for_store(store, fencing.BUCKET).get("sw").value
    text = raw.decode() if isinstance(raw, bytes) else raw

    assert text == json.dumps(json.loads(text), separators=(",", ":"))


def test_a_stored_document_missing_a_section_shares_none_with_other_swarms(store):
    saved = {key: value for key, value in fencing.EMPTY.items() if key != "fences"}
    state.for_store(store, fencing.BUCKET).create("old", json.dumps(saved).encode())

    fencing.write(store, "old", lambda doc: doc["fences"].update(probe={"sequence": 1}))

    assert fencing.read(store, "old")["fences"] == {"probe": {"sequence": 1}}
    assert fencing.read(store, "fresh")["fences"] == {}
    assert fencing.EMPTY["fences"] == {}


def test_a_refusal_count_leaves_the_fencing_document_unchanged(store):
    fencing.write(store, "sw", lambda doc: doc.update(epoch=2))
    before = fencing.read(store, "sw")
    fencing.bump(store, "sw", fencing.STALE_REJECTIONS)
    fencing.bump(store, "sw", fencing.STALE_REJECTIONS)
    assert fencing.read(store, "sw") == before
    assert fencing.count(store, "sw", fencing.STALE_REJECTIONS) == 2
    assert fencing.count(store, "sw", fencing.IDENTITY_CONFLICTS) == 0


def test_a_refusal_count_continues_from_the_legacy_redis_counter(store):
    store.redis.set(store.key("sw", fencing.IDENTITY_CONFLICTS), 4)
    assert fencing.count(store, "sw", fencing.IDENTITY_CONFLICTS) == 4
    fencing.bump(store, "sw", fencing.IDENTITY_CONFLICTS)
    store.redis.delete(store.key("sw", fencing.IDENTITY_CONFLICTS))
    assert fencing.count(store, "sw", fencing.IDENTITY_CONFLICTS) == 5


def test_a_refusal_count_retries_a_competing_bump(store, monkeypatch):
    counters = state.for_store(store, fencing.COUNTERS)
    real = counters.update
    raced = []

    def racing(key, value, revision):
        if not raced:
            raced.append(real(key, b"7", revision))
        return real(key, value, revision)

    monkeypatch.setattr(counters, "update", racing)
    monkeypatch.setattr(state, "for_store", lambda *args: counters)
    fencing.bump(store, "sw", fencing.STALE_REJECTIONS)
    fencing.bump(store, "sw", fencing.STALE_REJECTIONS)
    assert fencing.count(store, "sw", fencing.STALE_REJECTIONS) == 8


def test_a_refusal_count_that_keeps_conflicting_is_refused(store, monkeypatch):
    counters = state.for_store(store, fencing.COUNTERS)

    def conflict(*args):
        raise StateConflict("moved")

    monkeypatch.setattr(counters, "create", conflict)
    monkeypatch.setattr(state, "for_store", lambda *args: counters)
    with pytest.raises(SwarmError, match="kept changing"):
        fencing.bump(store, "sw", fencing.STALE_REJECTIONS)


def test_a_stale_lease_holder_cannot_write_executions(store, monkeypatch):
    from scripts.swarm.execution import ExecutionRegistry

    at = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda saved: at[0])
    first = lease.acquire(store, "sw", "home")
    at[0] = first.expires_at
    second = lease.acquire(store, "sw", "other")
    registry = ExecutionRegistry(store)
    with lease.fencing(first.epoch):
        with pytest.raises(SwarmError, match="the controller lease is stale"):
            registry.archive("sw")
        with pytest.raises(SwarmError, match="the controller lease is stale"):
            registry.set_pane("sw", "missing", "%1")
        agent = AgentRecord("eng-a", "eng", "task", seat="eng-1@sw")
        with pytest.raises(SwarmError, match="the controller lease is stale"):
            registry.update("sw", agent)
        with pytest.raises(SwarmError, match="the controller lease is stale"):
            registry.end_orphan("sw", agent, 0)
        with pytest.raises(SwarmError, match="the controller lease is stale"):
            store.drop_agent("sw", "eng-a")
    assert fencing.read(store, "sw")["executions"] == {}
    with lease.fencing(second.epoch):
        assert registry.archive("sw") == 0


def test_latest_seat_keeps_a_newer_generation_and_uses_the_last_equal_generation():
    from scripts.swarm.execution import latest

    newer = {"seat": "eng-1@sw", "generation": 2, "name": "new"}
    older = {"seat": "eng-1@sw", "generation": 1, "name": "old"}
    tied = {**newer, "name": "tie"}
    assert latest([newer, older], "seat") == {"eng-1@sw": newer}
    assert latest([newer, tied], "seat") == {"eng-1@sw": tied}


@pytest.mark.parametrize("use_bus", [False, True])
def test_removing_a_swarm_clears_its_fencing_domain_and_preserves_the_epoch(store, request, use_bus):
    from scripts.swarm.store import SwarmConfig

    if use_bus:
        request.getfixturevalue("bus")
    config = SwarmConfig("sw", "/repo", 1, 1)
    store.create(config)
    held = lease.acquire(store, "sw", "home")
    fencing.write(store, "sw", lambda doc: doc["orphans"].update({"old": "execution"}))
    store.remove("sw")
    doc = fencing.read(store, "sw")
    assert doc == {**fencing.EMPTY, "epoch": held.epoch}
    store.create(config)
    replacement = lease.acquire(store, "sw", "other")
    assert replacement.epoch > held.epoch
