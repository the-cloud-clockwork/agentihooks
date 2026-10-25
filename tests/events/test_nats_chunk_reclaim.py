import asyncio
import datetime
import json
import random
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts.events import nats, nats_payload, nats_state
from scripts.events.bus import StateConflict
from scripts.events.config import BusConfig
from scripts.events.subjects import subject
from tests.events import nats_server

pytestmark = pytest.mark.unit

HISTORY = 3
UPDATES = 8


@pytest.fixture
def bus(tmp_path):
    with nats_server.running(tmp_path / "jetstream") as url:
        opened = nats.open_bus(BusConfig(backend="nats", url=url))
        try:
            yield opened
        finally:
            opened.close()


def open_store(bus, scoped, monkeypatch):
    if scoped:
        monkeypatch.setattr(nats_state, "HISTORY", HISTORY, raising=False)
        store = nats_state.ScopedStateStore(bus, "crew", "reclaim")
        return store, store._stream, f"{subject('crew', 'state', 'reclaim')}._chunks.>"
    monkeypatch.setattr(nats, "KV_HISTORY", HISTORY)
    return bus.state("reclaim"), "KV_reclaim", "$KV.reclaim._chunks.>"


def chunk_bytes(bus, stream, pattern) -> int:
    info = bus._loop.run(bus._js.stream_info(stream, subjects_filter=pattern))
    names = info.state.subjects or {}
    return sum(len(bus._loop.run(bus._js.get_last_msg(stream, name)).data) for name in names)


def large(bus, seed):
    return random.Random(seed).randbytes(bus._nc.max_payload - nats_payload.KV_HEADER_BUDGET + 1)


@pytest.mark.timeout(5, method="thread", func_only=True)
@pytest.mark.parametrize("scoped", [False, True])
def test_repeated_large_updates_keep_only_chunks_of_retained_revisions(bus, scoped, monkeypatch):
    store, stream, pattern = open_store(bus, scoped, monkeypatch)
    neighbour = large(bus, "neighbour")
    store.create("neighbour", neighbour)
    single = chunk_bytes(bus, stream, pattern)
    values = [large(bus, index) for index in range(UPDATES + 1)]
    revisions = [store.create("deadlines", values[0])]
    for value in values[1:]:
        revisions.append(store.update("deadlines", value, revisions[-1]))
    stored = chunk_bytes(bus, stream, pattern)
    bound = (HISTORY + 1) * (len(values[0]) + nats_payload.KV_HEADER_BUDGET)
    assert stored <= bound, f"stored chunk bytes {stored} after {UPDATES} large updates exceed {bound}"
    assert stored == (HISTORY + 1) * single == 4 * 1047903
    retained = [(row.value, row.revision) for row in store.watch("deadlines")]
    assert retained == list(zip(values, revisions, strict=True))[-HISTORY:]
    assert store.get("deadlines").value == values[-1]
    assert store.get("neighbour").value == neighbour


@pytest.mark.parametrize("scoped", [False, True])
def test_a_refused_large_create_leaves_no_chunks_behind(bus, scoped, monkeypatch):
    store, stream, pattern = open_store(bus, scoped, monkeypatch)
    first = large(bus, "first")
    store.create("deadlines", first)
    before = chunk_bytes(bus, stream, pattern)
    with pytest.raises(StateConflict):
        store.create("deadlines", large(bus, "second"))
    assert chunk_bytes(bus, stream, pattern) == before
    assert store.get("deadlines").value == first


def test_a_stale_large_update_leaves_no_chunks_behind(bus, monkeypatch):
    store, stream, pattern = open_store(bus, False, monkeypatch)
    revision = store.create("deadlines", b"small")
    current = store.update("deadlines", large(bus, "current"), revision)
    before = chunk_bytes(bus, stream, pattern)
    with pytest.raises(StateConflict):
        store.update("deadlines", large(bus, "stale"), revision)
    assert chunk_bytes(bus, stream, pattern) == before
    assert store.get("deadlines").revision == current


def test_future_revision_conflict_preserves_an_inflight_writer(bus, monkeypatch):
    store, stream, pattern = open_store(bus, False, monkeypatch)
    revision = store.create("deadlines", b"small")
    value = large(bus, "inflight")
    refused = large(bus, "future")

    async def overlap():
        limit = bus._nc.max_payload - nats_payload.KV_HEADER_BUDGET
        staged = await nats_payload.encode(store._create_chunk, value, limit, nats_payload.scope("deadlines", revision))
        before = await bus._js.stream_info(stream, subjects_filter=pattern)
        with pytest.raises(StateConflict):
            await store._write("deadlines", refused, revision + 100, "future revision")
        after = await bus._js.stream_info(stream, subjects_filter=pattern)
        assert after.state.bytes == before.state.bytes
        assert await nats_payload.decode(store._read_chunk, staged) == value
        return await store._kv.update("deadlines", staged, last=revision)

    current = bus._loop.run(overlap())
    assert store.get("deadlines").value == value
    assert store.get("deadlines").revision == current


def test_reclaim_purges_only_unreferenced_chunks_written_before_the_head():
    owner = nats_payload.scope("deadlines", 0).split(".")[0]
    pattern = f"pre._chunks.{owner}."
    subjects = {f"{pattern}{name}": 1 for name in ("3.kept.0", "3.kept.1", "4.gone.0", "4.gone.1", "5.flight.0")}
    js = SimpleNamespace(
        stream_info=mock.AsyncMock(return_value=SimpleNamespace(state=SimpleNamespace(subjects=subjects))),
        purge_stream=mock.AsyncMock(),
    )
    referenced = nats_payload.PAGED + json.dumps([f"{owner}.3.kept", 2]).encode()
    history = mock.AsyncMock(return_value=[b"plain", b"", referenced, referenced])
    asyncio.run(nats_payload.reclaim(js, "stream", "pre.", "deadlines", history, 5))
    js.stream_info.assert_awaited_once_with("stream", subjects_filter=f"{pattern}>")
    assert js.purge_stream.await_args_list == [
        mock.call("stream", subject=f"{pattern}4.gone.0"),
        mock.call("stream", subject=f"{pattern}4.gone.1"),
    ]


def test_reclaim_skips_history_when_no_chunk_precedes_the_head():
    owner = nats_payload.scope("deadlines", 7).split(".")[0]
    state = SimpleNamespace(subjects={f"p._chunks.{owner}.7.x.0": 1})
    js = SimpleNamespace(
        stream_info=mock.AsyncMock(return_value=SimpleNamespace(state=state)),
        purge_stream=mock.AsyncMock(),
    )
    history = mock.AsyncMock()
    asyncio.run(nats_payload.reclaim(js, "s", "p.", "deadlines", history, 7))
    state.subjects = None
    asyncio.run(nats_payload.reclaim(js, "s", "p.", "deadlines", history, 9))
    history.assert_not_awaited()
    js.purge_stream.assert_not_awaited()


def test_chunk_scopes_separate_keys_and_bases():
    assert nats_payload.scope("a", 1) == f"{nats_payload.scope('a', 0).split('.')[0]}.1"
    assert nats_payload.scope("a", 0) != nats_payload.scope("b", 0)
    assert len(nats_payload.scope("a", 0).split(".")[0]) == 32


@pytest.mark.parametrize("value", [b"plain", nats_payload.COMPRESSED + b"compressed"])
def test_discard_keeps_nonpaged_values(value):
    js = SimpleNamespace(purge_stream=mock.AsyncMock())
    asyncio.run(nats_payload.discard(js, "stream", "pre.", value))
    js.purge_stream.assert_not_awaited()


def test_discard_removes_only_the_failed_write_token():
    js = SimpleNamespace(purge_stream=mock.AsyncMock())
    value = nats_payload.PAGED + json.dumps(["owner.42.failed", 3]).encode()
    asyncio.run(nats_payload.discard(js, "stream", "pre.", value))
    js.purge_stream.assert_awaited_once_with("stream", subject="pre._chunks.owner.42.failed.>")


@pytest.mark.parametrize("store_type", [nats.NatsStateStore, nats_state.ScopedStateStore])
def test_history_visits_each_sequence_and_preserves_empty_values(store_type):
    fetch = mock.AsyncMock(side_effect=[SimpleNamespace(seq=1, data=b"first"), SimpleNamespace(seq=2, data=None), None])
    store = SimpleNamespace(_next=fetch)
    assert asyncio.run(store_type._history(store, "key")) == [b"first", b""]
    assert fetch.await_args_list == [mock.call("key", 1), mock.call("key", 2), mock.call("key", 3)]


def test_a_losing_chunk_create_returns_quietly():
    create = mock.AsyncMock(side_effect=[1, nats.KeyWrongLastSequenceError()])
    store = SimpleNamespace(_kv=SimpleNamespace(create=create))
    assert asyncio.run(nats.NatsStateStore._create_chunk(store, "chunk", b"first")) is None
    assert asyncio.run(nats.NatsStateStore._create_chunk(store, "chunk", b"second")) is None
    assert create.await_args_list == [mock.call("chunk", b"first"), mock.call("chunk", b"second")]


def test_scoped_reclaim_compares_local_sequences_after_migration():
    reclaim = mock.AsyncMock()
    write = mock.AsyncMock(return_value=109)
    store = SimpleNamespace(
        _root=SimpleNamespace(_nc=SimpleNamespace(max_payload=2048)),
        _create_chunk=mock.AsyncMock(),
        _write_raw=write,
        _revision_base=100,
        _reclaim=reclaim,
        _swept=datetime.datetime.now(datetime.UTC),
    )
    assert asyncio.run(nats_state.ScopedStateStore._write(store, "key", b"small", 3)) == 109
    write.assert_awaited_once_with("key", b"small", 3)
    reclaim.assert_awaited_once_with("key", 9)


def test_paged_tokens_use_a_fixed_nonce_budget(monkeypatch):
    nonce = mock.Mock(return_value="nonce")
    monkeypatch.setattr(nats_payload.secrets, "token_hex", nonce)
    write = mock.AsyncMock()
    value = bytes(range(256)) * 8
    encoded = asyncio.run(nats_payload.encode(write, value, 128, "owner.3"))
    nonce.assert_called_once_with(16)
    token, count = json.loads(encoded[len(nats_payload.PAGED) :])
    assert token == "owner.3.nonce"
    assert count == write.await_count
    assert [call.args[0] for call in write.await_args_list] == [f"_chunks.owner.3.nonce.{i}" for i in range(count)]


def staged_bytes(bus, stream, prefix, value) -> int:
    token = json.loads(value[len(nats_payload.PAGED) :])[0]
    return chunk_bytes(bus, stream, f"{prefix}_chunks.{token}.>")


@pytest.mark.parametrize("scoped", [False, True])
def test_a_crashed_writer_on_an_idle_key_leaves_no_chunks_after_the_idle_time(bus, scoped, monkeypatch):
    store, stream, pattern = open_store(bus, scoped, monkeypatch)
    monkeypatch.setattr(nats_payload, "IDLE", datetime.timedelta(seconds=1), raising=False)
    prefix = pattern.removesuffix("_chunks.>")
    limit = bus._nc.max_payload - nats_payload.KV_HEADER_BUDGET
    kept = large(bus, "kept")
    store.create("kept", kept)
    retained = chunk_bytes(bus, stream, pattern)
    revision = store.create("idle", b"small")
    scope = nats_payload.scope("idle", revision)
    crashed = bus._loop.run(nats_payload.encode(store._create_chunk, large(bus, "crashed"), limit, scope))
    assert staged_bytes(bus, stream, prefix, crashed) > 0
    time.sleep(1.5)
    live = large(bus, "live")
    staged = bus._loop.run(nats_payload.encode(store._create_chunk, live, limit, nats_payload.scope("live", 0)))
    store.create("other", b"small")
    left = staged_bytes(bus, stream, prefix, crashed)
    assert left == 0, f"orphan staged chunk bytes on an idle key after the idle time: {left}"
    assert bus._loop.run(nats_payload.decode(store._read_chunk, staged)) == live
    assert store.get("kept").value == kept
    assert store.get("idle").value == b"small"
    assert chunk_bytes(bus, stream, pattern) - staged_bytes(bus, stream, prefix, staged) == retained


def test_sweep_purges_only_idle_unreferenced_chunk_groups():
    now = datetime.datetime(2026, 10, 25, tzinfo=datetime.UTC)
    owner, gone = nats_payload.scope("held", 0).split(".")[0], nats_payload.scope("gone", 0).split(".")[0]
    names = ["held", f"_chunks.{owner}.1.kept.0", f"_chunks.{owner}.1.old.0", f"_chunks.{owner}.1.old.10"]
    names += [f"_chunks.{owner}.1.old.9", f"_chunks.{owner}.2.young.0", f"_chunks.{gone}.0.lost.0"]
    ages = {"kept.0": 60, "old.10": 16, "young.0": 14, "lost.0": 15}
    js = SimpleNamespace(
        stream_info=mock.AsyncMock(
            return_value=SimpleNamespace(state=SimpleNamespace(subjects={f"p.{name}": 1 for name in names}))
        ),
        get_last_msg=mock.AsyncMock(
            side_effect=lambda stream, name: SimpleNamespace(
                time=now - datetime.timedelta(minutes=ages[".".join(name.split(".")[-2:])])
            )
        ),
        purge_stream=mock.AsyncMock(),
    )
    referenced = nats_payload.PAGED + json.dumps([f"{owner}.1.kept", 1]).encode()
    history = mock.AsyncMock(return_value=[b"plain", referenced, referenced])
    asyncio.run(nats_payload.sweep(js, "s", "p.", history, now))
    js.stream_info.assert_awaited_once_with("s", subjects_filter="p.>")
    history.assert_awaited_once_with("held")
    assert sorted(call.kwargs["subject"] for call in js.purge_stream.await_args_list) == sorted(
        f"p.{name}" for name in names[2:5] + names[6:]
    )
    assert all(call.args == ("s",) for call in js.purge_stream.await_args_list)
    js.stream_info.return_value.state.subjects = None
    js.purge_stream.reset_mock()
    asyncio.run(nats_payload.sweep(js, "s", "p.", history, now))
    js.purge_stream.assert_not_awaited()


def test_sweep_is_due_once_per_idle_period():
    now = datetime.datetime(2026, 10, 25, tzinfo=datetime.UTC)
    assert nats_payload.due(None, now) == now
    assert nats_payload.due(now - nats_payload.IDLE + datetime.timedelta(microseconds=1), now) is None
    assert nats_payload.due(now - nats_payload.IDLE, now) == now
    assert nats_payload.IDLE == datetime.timedelta(minutes=15)


def sweeping_store(scoped):
    js = SimpleNamespace(_nc=SimpleNamespace(max_payload=2048))
    common = {"_create_chunk": mock.AsyncMock(), "_reclaim": mock.AsyncMock(), "_history": mock.AsyncMock()}
    if scoped:
        root = SimpleNamespace(_nc=js._nc, _js=js)
        store = SimpleNamespace(
            _root=root, _swarm="crew", _bucket="b", _stream="S", _revision_base=0, _swept=None, **common
        )
        store._write_raw = mock.AsyncMock(return_value=1)
        return store, lambda: nats_state.ScopedStateStore._write(store, "k", b"small", 0), store._history
    store = SimpleNamespace(_js=js, _bucket="b", _swept=None, _key_history=mock.AsyncMock(), **common)
    store._kv = SimpleNamespace(create=mock.AsyncMock(return_value=1))
    return store, lambda: nats.NatsStateStore._write(store, "k", b"small", 0, "conflict"), store._key_history


@pytest.mark.parametrize("scoped", [False, True])
def test_writes_sweep_the_store_once_per_idle_period(scoped, monkeypatch):
    sweep = mock.AsyncMock()
    monkeypatch.setattr(nats_payload, "sweep", sweep)
    store, write, history = sweeping_store(scoped)
    asyncio.run(write())
    asyncio.run(write())
    sweep.assert_awaited_once()
    js, stream, prefix, called, now = sweep.await_args.args
    assert (js, called, now) == (store._root._js if scoped else store._js, history, store._swept)
    assert (stream, prefix) == (("S", f"{subject('crew', 'state', 'b')}.") if scoped else ("KV_b", "$KV.b."))
    assert datetime.datetime.now(datetime.UTC) - now < datetime.timedelta(minutes=1)


def test_kv_key_history_reads_the_key_subject():
    store = SimpleNamespace(_bucket="b", _history=mock.AsyncMock(return_value=[b"v"]))
    assert asyncio.run(nats.NatsStateStore._key_history(store, "k")) == [b"v"]
    store._history.assert_awaited_once_with("$KV.b.k")
