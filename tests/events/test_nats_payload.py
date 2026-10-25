import random
from dataclasses import replace
from unittest import mock

import pytest

from scripts.events import nats
from scripts.events.bus import StateConflict
from scripts.events.config import BusConfig
from tests.events import nats_server
from tests.events.test_nats import event

pytestmark = pytest.mark.unit


@pytest.fixture
def bus(tmp_path):
    with nats_server.running(tmp_path / "jetstream") as url:
        opened = nats.open_bus(BusConfig(backend="nats", url=url))
        try:
            yield opened
        finally:
            opened.close()


def test_large_state_round_trips_with_atomic_revisions_and_history(bus):
    store = bus.state("hub-timers")
    first = random.Random(1).randbytes(2 * 1024 * 1024)
    second = first + b"changed"
    revision = store.create("deadlines", first)
    assert store.get("deadlines").value == first
    updated = store.update("deadlines", second, revision)
    assert store.get("deadlines").revision == updated
    assert store.get("deadlines").value == second
    with pytest.raises(StateConflict):
        store.update("deadlines", b"stale", revision)
    assert [(row.value, row.revision) for row in store.watch("deadlines")] == [
        (first, revision),
        (second, updated),
    ]
    assert not bus._nc.is_closed


def test_event_headers_cannot_push_a_publish_over_the_server_limit(bus, monkeypatch):
    sample = event("large")
    oversized = replace(sample, data={"padding": "x" * (bus._nc.max_payload - len(sample.encode()) - 30)})
    assert len(oversized.encode()) < bus._nc.max_payload
    publish = mock.AsyncMock()
    monkeypatch.setattr(bus._js, "publish", publish)
    with pytest.raises(ValueError, match=r"swarm\.s\.task\.claimed.*payload"):
        bus.events.publish("swarm.s.task.claimed", oversized)
    publish.assert_not_awaited()
    assert not bus._nc.is_closed
