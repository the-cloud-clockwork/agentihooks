import random

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


@pytest.mark.parametrize("scoped", [False, True])
def test_repeated_large_updates_keep_only_chunks_of_retained_revisions(bus, scoped, monkeypatch):
    store, stream, pattern = open_store(bus, scoped, monkeypatch)
    neighbour = large(bus, "neighbour")
    store.create("neighbour", neighbour)
    values = [large(bus, index) for index in range(UPDATES + 1)]
    revisions = [store.create("deadlines", values[0])]
    for value in values[1:]:
        revisions.append(store.update("deadlines", value, revisions[-1]))
    stored = chunk_bytes(bus, stream, pattern)
    bound = (HISTORY + 1) * (len(values[0]) + nats_payload.KV_HEADER_BUDGET)
    assert stored <= bound, f"stored chunk bytes {stored} after {UPDATES} large updates exceed {bound}"
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
