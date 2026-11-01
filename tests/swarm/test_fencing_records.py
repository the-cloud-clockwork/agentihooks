import json
from typing import Any

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def store() -> Any:
    import fakeredis

    from scripts.swarm.store import RedisStore

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from scripts.swarm import fencing, lease

    at = [1000]
    monkeypatch.setattr(fencing, "now_ms", lambda: at[0])
    monkeypatch.setattr(lease, "now_ms", lambda saved: at[0])
    return at


def _row(execution_id: str, name: str, generation: int, **extra: Any) -> dict[str, Any]:
    return {"execution_id": execution_id, "name": name, "seat": "eng-1", "generation": generation, **extra}


def _migrated(store: Any) -> Any:
    from scripts.swarm import fencing

    fencing.migrate_records(store, "sw", writers_upgraded=True)
    return fencing.records(store, "sw")


def _document(store: Any) -> Any:
    from scripts.events.redis_state import RedisStateStore
    from scripts.swarm import fencing

    return RedisStateStore(store.redis, fencing.BUCKET).get("sw")


def _log(store: Any) -> list[dict[str, str]]:
    from scripts.tick.adapters.records_redis import RedisRecords

    return [fields for _, fields in store.redis.xrange(RedisRecords(store.redis).log_key("sw"))]


def test_every_record_of_a_swarm_shares_its_hash_tag(store: Any) -> None:
    from scripts.swarm.store import PREFIX
    from scripts.tick.adapters.records_redis import RedisRecords

    port = RedisRecords(store.redis, "root")
    assert port.key("sw", "executions") == "root:{sw}:fence:executions"
    assert port.revision_key("sw") == "root:{sw}:fence:revision"
    assert port.log_key("sw") == "root:{sw}:fence:log"
    assert RedisRecords(store.redis).key("sw", "meta") == f"{PREFIX}:{{sw}}:fence:meta"


def test_both_layouts_answer_the_records_port(store: Any) -> None:
    from scripts.swarm import fencing
    from scripts.tick.ports.records import Records

    whole = fencing.records(store, "sw")
    assert isinstance(whole, fencing.WholeDocumentRecords)
    assert whole.layout("sw") == fencing.DOCUMENT
    port = _migrated(store)
    assert isinstance(port, Records)
    assert port.layout("sw") == fencing.RECORDS


def test_migration_copies_the_document_into_records_and_reads_it_back(store: Any, clock: list[int]) -> None:
    from scripts.swarm import fencing, lease

    held = lease.acquire(store, "sw", "home")
    assert held is not None
    fencing.write(store, "sw", lambda doc: doc["executions"].update(a=_row("a", "eng", 1)))
    fencing.write(store, "sw", lambda doc: doc["archive"].update(z=_row("z", "old", 0)))
    fencing.write(store, "sw", lambda doc: doc["claims"].update(t1={"agent": "eng"}))
    before = fencing.read(store, "sw")

    written = fencing.migrate_records(store, "sw", writers_upgraded=True)

    after = fencing.read(store, "sw")
    assert written == 8
    assert after["archive_ref"] == "records:sw"
    assert {key: value for key, value in after.items() if key != "archive_ref"} == {
        key: value for key, value in before.items() if key != "archive_ref"
    }
    assert fencing.migrate_records(store, "sw", writers_upgraded=True) == 0


def test_migration_waits_for_every_writer_and_the_redis_backend(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.events import state
    from scripts.swarm import fencing
    from scripts.swarm.store import SwarmError

    with pytest.raises(SwarmError, match="upgrade every live fencing reader and writer"):
        fencing.migrate_records(store, "sw")
    monkeypatch.setenv(state.BACKEND_ENV, "bus")
    with pytest.raises(SwarmError, match="need the Redis state backend"):
        fencing.migrate_records(store, "sw", writers_upgraded=True)
    assert fencing.records(store, "sw").layout("sw") == fencing.DOCUMENT


def test_migration_retries_when_the_document_changes_under_it(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import fencing

    real = fencing.hydrate_archive
    calls: list[int] = []

    def racing(saved: Any, doc: dict[str, Any]) -> None:
        real(saved, doc)
        if not calls:
            fencing.write(store, "sw", lambda raced: raced.update(epoch=7))
        calls.append(1)

    fencing.read(store, "sw")
    monkeypatch.setattr(fencing, "hydrate_archive", racing)
    fencing.migrate_records(store, "sw", writers_upgraded=True)
    assert len(calls) == 2
    assert fencing.read(store, "sw")["epoch"] == 7


def test_a_pass_on_records_writes_no_whole_document(store: Any, clock: list[int]) -> None:
    from scripts.swarm import fencing, lease

    held = lease.acquire(store, "sw", "home")
    assert held is not None
    _migrated(store)
    document = _document(store)

    lease.fenced(store, "sw", held, lambda doc: doc["executions"].update(a=_row("a", "eng", 1)))
    lease.fenced(store, "sw", held.epoch, lambda doc: doc["intents"].update(i={"epoch": held.epoch}))
    clock[0] += lease.ttl_ms() - lease.tick_ms()
    renewed = lease.renew(store, "sw", held)

    assert _document(store) == document
    assert fencing.read(store, "sw")["executions"] == {"a": _row("a", "eng", 1)}
    assert fencing.read(store, "sw")["intents"] == {"i": {"epoch": held.epoch}}
    assert fencing.read_lease(store, "sw") == {"lease": renewed.__dict__}
    assert fencing.read_executions(store, "sw") == {"a": _row("a", "eng", 1)}


def test_a_stale_controller_racing_a_takeover_is_refused_at_the_write(store: Any, clock: list[int]) -> None:
    from scripts.swarm import fencing, lease
    from scripts.swarm.store import SwarmError

    first = lease.acquire(store, "sw", "home")
    assert first is not None
    _migrated(store)
    clock[0] = first.expires_at - 1
    taken: list[Any] = []

    def stale(doc: dict[str, Any]) -> None:
        doc["executions"]["stale"] = _row("stale", "eng", 1)
        clock[0] = first.expires_at
        taken.append(lease.acquire(store, "sw", "other"))

    with pytest.raises(SwarmError, match="the controller lease is stale"):
        lease.fenced(store, "sw", first, stale)

    second = taken[0]
    assert len(taken) == 1
    assert second is not None and second.owner == "other" and second.epoch == first.epoch + 1
    assert lease.current(store, "sw") == second
    assert "stale" not in fencing.read(store, "sw")["executions"]
    lease.fenced(store, "sw", second, lambda doc: doc["executions"].update(live=_row("live", "eng", 2)))
    assert set(fencing.read(store, "sw")["executions"]) == {"live"}
    assert [entry["owner"] for entry in _log(store)][-1] == "other"


def test_two_writers_on_different_records_both_commit(store: Any) -> None:
    from scripts.swarm import fencing

    _migrated(store)
    inner: list[int] = []

    def outer(doc: dict[str, Any]) -> None:
        if not inner:
            inner.append(1)
            fencing.write(store, "sw", lambda other: other["grants"].update(g={"n": 1}))
        doc["registrations"]["r"] = {"n": 2}

    fencing.write(store, "sw", outer)
    doc = fencing.read(store, "sw")
    assert doc["grants"] == {"g": {"n": 1}}
    assert doc["registrations"] == {"r": {"n": 2}}
    assert inner == [1]


def test_two_writers_on_one_record_retry_the_second(store: Any) -> None:
    from scripts.swarm import fencing

    _migrated(store)
    calls: list[int] = []

    def outer(doc: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 1:
            fencing.write(store, "sw", lambda other: other["grants"].update(g={"n": 1}))
        doc["grants"]["g"] = {"n": doc["grants"].get("g", {}).get("n", 0) + 10}

    fencing.write(store, "sw", outer)
    assert len(calls) == 2
    assert fencing.read(store, "sw")["grants"] == {"g": {"n": 11}}


def test_a_write_that_keeps_conflicting_gives_up(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import fencing
    from scripts.swarm.store import SwarmError

    _migrated(store)
    monkeypatch.setattr(fencing, "ATTEMPTS", 2)

    def outer(doc: dict[str, Any]) -> None:
        fencing.write(store, "sw", lambda other: other.update(epoch=other["epoch"] + 1))
        doc["epoch"] = 100

    with pytest.raises(SwarmError, match="kept changing"):
        fencing.write(store, "sw", outer)


def test_an_unchanged_write_sends_no_batch_and_returns_its_result(store: Any) -> None:
    from scripts.swarm import fencing

    _migrated(store)
    revision = store.redis.get(fencing.RedisRecords(store.redis).revision_key("sw"))
    assert fencing.write(store, "sw", lambda doc: "same") == "same"
    assert store.redis.get(fencing.RedisRecords(store.redis).revision_key("sw")) == revision


def test_the_name_index_follows_the_newest_execution(store: Any) -> None:
    from scripts.swarm import fencing

    port = _migrated(store)
    fencing.write(store, "sw", lambda doc: doc["executions"].update(a=_row("a", "eng", 1), b=_row("b", "eng", 2)))
    assert port.record("sw", fencing.BY_NAME, "eng").value == "b"
    fencing.write(store, "sw", lambda doc: doc["executions"].pop("b"))
    assert port.record("sw", fencing.BY_NAME, "eng").value == "a"
    fencing.write(store, "sw", lambda doc: doc["executions"].update(c={"execution_id": "c"}))
    fencing.write(store, "sw", lambda doc: doc["executions"].pop("a"))
    assert port.record("sw", fencing.BY_NAME, "eng") is None
    assert port.sections("sw", (fencing.BY_NAME,)) == {fencing.BY_NAME: {}}


def test_name_heads_pick_the_later_row_on_a_tie() -> None:
    from scripts.swarm import fencing

    rows = {
        "a": _row("a", "eng", 1),
        "b": _row("b", "eng", 1),
        "c": _row("c", "eng", 0),
        "d": {"name": 3, "generation": 9},
    }
    assert fencing._heads(rows) == {"eng": "b"}
    assert fencing._heads({"e": {"name": "x", "generation": 1.5}}) == {"x": "e"}


def test_moving_rows_into_the_archive_writes_only_the_moved_rows(store: Any) -> None:
    from scripts.swarm import fencing

    port = _migrated(store)
    fencing.write(store, "sw", lambda doc: doc["archive"].update({str(i): _row(str(i), "old", 0) for i in range(5)}))
    fencing.write(store, "sw", lambda doc: doc["executions"].update(a=_row("a", "eng", 1)))
    count = len(_log(store))

    def move(doc: dict[str, Any]) -> None:
        fencing.hydrate_archive(store, doc)
        doc["archive"]["a"] = doc["executions"].pop("a")
        doc["archive"]["0"]["note"] = "kept"

    fencing.write(store, "sw", move)
    entries = _log(store)
    assert len(entries) == count + 1
    assert entries[-1]["records"] == "5"
    assert port.record("sw", "archive", "a").value == _row("a", "eng", 1)
    assert fencing.read(store, "sw")["archive"]["0"]["note"] == "kept"
    bare = {"executions": {}, "archive": {}, "archive_ref": "records:sw"}
    assert fencing.execution_row(store, bare, "a") == _row("a", "eng", 1)
    assert fencing.execution_row(store, bare, "none") is None


def test_dropping_archive_rows_after_hydrating_deletes_them(store: Any) -> None:
    from scripts.swarm import fencing

    port = _migrated(store)
    fencing.write(store, "sw", lambda doc: doc["archive"].update(x=_row("x", "old", 0), y=_row("y", "old", 0)))

    def drop(doc: dict[str, Any]) -> None:
        fencing.hydrate_archive(store, doc)
        doc["archive"].pop("x")

    fencing.write(store, "sw", drop)
    assert port.record("sw", "archive", "x") is None
    assert port.record("sw", "archive", "y") is not None


def test_authority_reads_single_records(store: Any) -> None:
    from scripts.swarm import fencing

    _migrated(store)
    fencing.write(store, "sw", lambda doc: doc["claims"].update(t1={"a": 1}))
    fencing.write(store, "sw", lambda doc: doc["journals"].update(t2=[{"a": 2}]))
    assert fencing.authority_held(store, "sw", "t1")
    assert fencing.authority_held(store, "sw", "t2")
    assert not fencing.authority_held(store, "sw", "t3")


def test_scalars_are_put_and_removed_as_meta_records(store: Any) -> None:
    from scripts.swarm import fencing

    port = _migrated(store)
    fencing.write(store, "sw", lambda doc: doc.update(grants_disabled="yes", extra=[1]))
    assert port.record("sw", "meta", "grants_disabled").value == "yes"
    assert fencing.read(store, "sw")["extra"] == [1]
    fencing.write(store, "sw", lambda doc: doc.pop("extra"))
    assert port.record("sw", "meta", "extra") is None
    assert "extra" not in fencing.read(store, "sw")


def test_a_saved_export_restores_onto_records(store: Any) -> None:
    from scripts.swarm import fencing

    _migrated(store)
    fencing.write(store, "sw", lambda doc: doc["archive"].update(x=_row("x", "old", 0)))
    fencing.write(store, "sw", lambda doc: doc["grants"].update(g={"n": 1}))
    saved = fencing.read(store, "sw")
    fencing.write(store, "sw", lambda doc: doc["grants"].update(h={"n": 2}))
    fencing.write(store, "sw", lambda doc: doc["archive"].update(y=_row("y", "old", 0)))

    fencing.replace(store, "sw", saved)

    restored = fencing.read(store, "sw")
    assert restored["grants"] == {"g": {"n": 1}}
    assert set(restored["archive"]) == {"x"}
    assert restored["archive_ref"] == "records:sw"


def test_a_records_write_aborts_a_watched_transaction(store: Any) -> None:
    from redis.exceptions import WatchError

    from scripts.swarm import fencing

    _migrated(store)
    with store.redis.pipeline() as pipe:
        fencing.watch(store, pipe, "sw")
        fencing.write(store, "sw", lambda doc: doc.update(epoch=3))
        pipe.multi()
        pipe.set("probe", "1")
        with pytest.raises(WatchError):
            pipe.execute()
    with store.redis.pipeline() as pipe:
        fencing.watch(store, pipe, "other")
        fencing.write(store, "sw", lambda doc: doc.update(epoch=4))
        pipe.multi()
        pipe.set("probe", "2")
        pipe.execute()
    assert store.redis.get("probe") == "2"


def test_a_document_write_aborts_a_watched_transaction(store: Any) -> None:
    from redis.exceptions import WatchError

    from scripts.swarm import fencing

    with store.redis.pipeline() as pipe:
        fencing.watch(store, pipe, "sw")
        fencing.write(store, "sw", lambda doc: doc.update(epoch=3))
        pipe.multi()
        pipe.set("probe", "1")
        with pytest.raises(WatchError):
            pipe.execute()


def test_two_writers_on_one_hydrated_archive_row_retry_the_second(store: Any) -> None:
    from scripts.swarm import fencing

    _migrated(store)
    fencing.write(store, "sw", lambda doc: doc["archive"].update(x={"a": 1, "b": 1}))
    calls: list[int] = []

    def outer(doc: dict[str, Any]) -> None:
        calls.append(1)
        fencing.hydrate_archive(store, doc)
        if len(calls) == 1:

            def inner(other: dict[str, Any]) -> None:
                fencing.hydrate_archive(store, other)
                other["archive"]["x"]["a"] = 2

            fencing.write(store, "sw", inner)
        doc["archive"]["x"]["b"] = 2

    fencing.write(store, "sw", outer)
    assert len(calls) == 2
    assert fencing.read(store, "sw")["archive"]["x"] == {"a": 2, "b": 2}


def test_a_migration_racing_another_migration_writes_nothing(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import fencing

    fencing.read(store, "sw")
    real = fencing.hydrate_archive
    calls: list[int] = []

    def racing(saved: Any, doc: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 1:
            monkeypatch.setattr(fencing, "hydrate_archive", real)
            fencing.migrate_records(store, "sw", writers_upgraded=True)
        real(saved, doc)

    monkeypatch.setattr(fencing, "hydrate_archive", racing)
    assert fencing.migrate_records(store, "sw", writers_upgraded=True) == 0
    assert calls == [1]
    assert fencing.records(store, "sw").layout("sw") == fencing.RECORDS


def test_a_whole_document_batch_keeps_the_external_archive(store: Any) -> None:
    from scripts.swarm import fencing
    from scripts.tick.ports.records import FencedBatch, RecordWrite

    fencing.write(store, "sw", lambda doc: doc["archive"].update(z=_row("z", "old", 0)))
    fencing.migrate_archive(store, "sw", writers_upgraded=True)
    port = fencing.WholeDocumentRecords(store)
    port.fenced_batch("sw", FencedBatch((RecordWrite("put", "grants", "g", 0, {"n": 1}),)))
    doc = fencing.read(store, "sw")
    assert doc["grants"] == {"g": {"n": 1}}
    assert doc["archive"] == {"z": _row("z", "old", 0)}


def test_the_fenced_batch_checks_the_lease_and_writes_all_or_nothing(store: Any) -> None:
    from scripts.tick.adapters.records_redis import RedisRecords
    from scripts.tick.ports.records import (
        BLIND,
        FencedBatch,
        LeaseFence,
        RecordWrite,
        RevisionConflict,
        StaleLease,
    )

    port = RedisRecords(store.redis)
    lease = {"owner": "home", "epoch": 2, "expires_at": 500}
    port.fenced_batch("sw", FencedBatch((RecordWrite("put", "meta", "lease", 0, lease),)))
    good = RecordWrite("put", "grants", "g", 0, {"n": 1})
    for fence in (LeaseFence("other", 2, 100), LeaseFence("home", 3, 100), LeaseFence("home", 2, 500)):
        with pytest.raises(StaleLease, match="FENCE_STALE"):
            port.fenced_batch("sw", FencedBatch((good,), fence))
    with pytest.raises(RevisionConflict, match="FENCE_CONFLICT"):
        port.fenced_batch("sw", FencedBatch((good, RecordWrite("put", "grants", "h", 9, {"n": 2}))))
    assert port.sections("sw", ("grants",)) == {"grants": {}}

    revision = port.fenced_batch("sw", FencedBatch((good,), LeaseFence("", 2, 499)))
    assert port.record("sw", "grants", "g").__dict__ == {"revision": revision, "value": {"n": 1}}
    with pytest.raises(RevisionConflict):
        port.fenced_batch("sw", FencedBatch((RecordWrite("check", "grants", "g", 0),)))
    port.fenced_batch("sw", FencedBatch((RecordWrite("delete", "grants", "g", revision),)))
    port.fenced_batch("sw", FencedBatch((RecordWrite("delete", "grants", "gone", BLIND),)))
    assert port.record("sw", "grants", "g") is None
    port.fenced_batch("sw", FencedBatch((RecordWrite("put", "meta", "lease", revision - 1, None),)))
    with pytest.raises(StaleLease, match="no controller lease"):
        port.fenced_batch("sw", FencedBatch((good,), LeaseFence("home", 2, 1)))
    store.redis.hdel(port.key("sw", "meta"), "lease")
    with pytest.raises(StaleLease, match="no controller lease"):
        port.fenced_batch("sw", FencedBatch((good,), LeaseFence("home", 2, 1)))


def test_the_fence_log_is_a_capped_stream(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.tick.adapters import records_redis
    from scripts.tick.ports.records import BLIND, FencedBatch, RecordWrite

    monkeypatch.setattr(records_redis, "LOG_CAP", 3)
    port = records_redis.RedisRecords(store.redis)
    for index in range(5):
        port.fenced_batch("sw", FencedBatch((RecordWrite("put", "fences", "f", BLIND, index),)))
    entries = _log(store)
    assert [entry["revision"] for entry in entries] == ["3", "4", "5"]
    assert entries[-1] == {"revision": "5", "owner": "", "records": "1"}


def test_an_unrelated_script_error_is_raised_unchanged(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from redis.exceptions import ResponseError

    from scripts.tick.adapters.records_redis import RedisRecords
    from scripts.tick.ports.records import FencedBatch, RecordWrite

    port = RedisRecords(store.redis)
    store.redis.set(port.key("sw", "grants"), "not a hash")
    with pytest.raises(ResponseError) as raised:
        port.fenced_batch("sw", FencedBatch((RecordWrite("put", "grants", "g", 0, 1),)))
    assert "FENCE" not in str(raised.value)


def test_the_batch_call_lists_keys_and_arguments_in_script_order(store: Any) -> None:
    from scripts.tick.adapters.records_redis import RedisRecords
    from scripts.tick.ports.records import FencedBatch, LeaseFence, RecordWrite

    port = RedisRecords(store.redis, "p")
    keys, args = port.batch_call(
        "sw",
        FencedBatch(
            (RecordWrite("put", "grants", "g", 4, {"n": 1}), RecordWrite("delete", "claims", "t", 2)),
            LeaseFence("home", 3, 9),
        ),
    )
    assert keys == [
        "p:{sw}:fence:revision",
        "p:{sw}:fence:log",
        "p:{sw}:fence:meta",
        "p:{sw}:fence:grants",
        "p:{sw}:fence:claims",
    ]
    assert args == [
        json.dumps({"owner": "home", "epoch": 3, "now_ms": 9}),
        1000,
        "home",
        "g",
        4,
        "put",
        '{"n":1}',
        "t",
        2,
        "delete",
        "",
    ]
    assert port.batch_call("sw", FencedBatch(()))[1] == ["", 1000, ""]


def test_the_whole_document_port_reads_sections_and_records(store: Any) -> None:
    from scripts.swarm import fencing

    fencing.write(store, "sw", lambda doc: doc["executions"].update(a=_row("a", "eng", 1)))
    fencing.write(store, "sw", lambda doc: doc["archive"].update(z=_row("z", "old", 0)))
    port = fencing.WholeDocumentRecords(store)
    revision = _document(store).revision
    found = port.sections("sw", ("meta", "executions", "by-name", "archive"))
    assert found["executions"]["a"].__dict__ == {"revision": revision, "value": _row("a", "eng", 1)}
    assert found["by-name"]["eng"].value == "a"
    assert found["meta"]["epoch"].value == 0
    assert "executions" not in found["meta"]
    assert found["archive"]["z"].value == _row("z", "old", 0)
    assert port.record("sw", "executions", "missing") is None


def test_the_whole_document_port_fences_and_applies_a_batch(store: Any, clock: list[int]) -> None:
    from scripts.swarm import fencing, lease
    from scripts.tick.ports.records import BLIND, FencedBatch, LeaseFence, RecordWrite, RevisionConflict, StaleLease

    held = lease.acquire(store, "sw", "home")
    assert held is not None
    port = fencing.WholeDocumentRecords(store)
    revision = _document(store).revision
    fence = LeaseFence("home", held.epoch, clock[0])
    for stale in (
        LeaseFence("other", held.epoch, clock[0]),
        LeaseFence("home", held.epoch + 1, clock[0]),
        LeaseFence("home", held.epoch, held.expires_at),
    ):
        with pytest.raises(StaleLease):
            port.fenced_batch("sw", FencedBatch((), stale))
    with pytest.raises(RevisionConflict, match="grants/g is not at revision 3"):
        port.fenced_batch("sw", FencedBatch((RecordWrite("put", "grants", "g", 3, 1),), fence))
    with pytest.raises(RevisionConflict):
        port.fenced_batch("sw", FencedBatch((RecordWrite("check", "meta", "epoch", 0),)))

    written = port.fenced_batch(
        "sw",
        FencedBatch(
            (
                RecordWrite("put", "grants", "g", 0, {"n": 1}),
                RecordWrite("put", "meta", "grants_disabled", revision, "off"),
                RecordWrite("put", "by-name", "eng", 0, "ignored"),
                RecordWrite("put", "archive", "z", BLIND, _row("z", "old", 0)),
                RecordWrite("check", "meta", "epoch", revision),
            ),
            LeaseFence("", held.epoch, clock[0]),
        ),
    )
    doc = fencing.read(store, "sw")
    assert written == _document(store).revision
    assert doc["grants"] == {"g": {"n": 1}}
    assert doc["grants_disabled"] == "off"
    assert doc["archive"] == {"z": _row("z", "old", 0)}
    assert fencing.read_lease(store, "sw") == {"lease": held.__dict__}

    port.fenced_batch(
        "sw",
        FencedBatch(
            (RecordWrite("delete", "grants", "g", written), RecordWrite("delete", "meta", "grants_disabled", written))
        ),
    )
    doc = fencing.read(store, "sw")
    assert doc["grants"] == {}
    assert doc["grants_disabled"] == ""
    assert "grants_disabled" not in json.loads(_document(store).value)


def test_the_whole_document_port_refuses_a_concurrent_document_write(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.events.redis_state import RedisStateStore
    from scripts.swarm import fencing
    from scripts.tick.ports.records import BLIND, FencedBatch, RecordWrite, RevisionConflict

    port = fencing.WholeDocumentRecords(store)
    port.sections("sw", ("meta",))
    real = RedisStateStore.update

    def raced(self: RedisStateStore, key: str, value: bytes, revision: int) -> int:
        real(self, key, value, revision)
        return real(self, key, value, revision)

    monkeypatch.setattr(RedisStateStore, "update", raced)
    with pytest.raises(RevisionConflict):
        port.fenced_batch("sw", FencedBatch((RecordWrite("put", "grants", "g", BLIND, 1),)))


def test_the_migration_command_needs_the_writer_rollout(
    store: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import fencing, records_migration

    monkeypatch.setattr(records_migration.store, "connect", lambda: store)
    with pytest.raises(SystemExit):
        records_migration.main(["sw"])
    assert "upgrade every live fencing reader and writer" in capsys.readouterr().err
    records_migration.main(["sw", "--writers-upgraded"])
    assert capsys.readouterr().out.strip() == "1"
    assert fencing.records(store, "sw").layout("sw") == fencing.RECORDS
