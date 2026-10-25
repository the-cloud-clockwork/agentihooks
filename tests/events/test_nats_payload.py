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


@pytest.mark.parametrize("compressible", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
def test_large_state_round_trips_with_atomic_revisions_and_history(bus, compressible, scoped):
    if scoped:
        from scripts.events.nats_state import ScopedStateStore

        store = ScopedStateStore(bus, "crew", "hub-timers")
    else:
        store = bus.state("hub-timers")
    first = b"a" * (2 * 1024 * 1024) if compressible else random.Random(1).randbytes(2 * 1024 * 1024)
    second = first + b"changed"
    revision = store.create("deadlines", first)
    assert store.get("deadlines").value == first
    with pytest.raises(StateConflict):
        store.create("deadlines", first)
    rebound = (
        ScopedStateStore(bus, "crew", "hub-timers")
        if scoped
        else nats.NatsStateStore(bus._loop, bus._js, "hub-timers", False)
    )
    assert rebound.get("deadlines").value == first
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


@pytest.mark.parametrize(
    "value", [b"ordinary", b"\x00agentihooks-state-zlib:literal", b"\x00agentihooks-state-pages:literal"]
)
def test_state_preserves_legacy_values_and_reserved_prefixes(bus, value):
    store = bus.state("legacy")
    store.create("value", value)
    assert store.get("value").value == value
    assert [row.value for row in store.watch("value")] == [value]


def test_exact_encoded_header_budget_is_allowed_and_one_extra_byte_is_refused():
    from types import SimpleNamespace

    from scripts.events import nats_payload

    client = SimpleNamespace(max_payload=100)
    nats_payload.validate(client, "subject", b"x" * 100)
    with pytest.raises(ValueError, match="subject: payload 101 exceeds server limit 100"):
        nats_payload.validate(client, "subject", b"x" * 101)
    nats_payload.validate(client, "subject", b"x" * 81, {"x": "é"})
    with pytest.raises(ValueError, match="subject: payload 101 exceeds server limit 100"):
        nats_payload.validate(client, "subject", b"x" * 82, {"x": "é"})


def test_a_reconnecting_bus_serves_a_hub_after_the_server_closes_the_connection(tmp_path):
    import asyncio

    from scripts.hub import registry, runner

    with nats_server.running(tmp_path / "jetstream") as url:
        opened = nats.open_bus(BusConfig(backend="nats", url=url, reconnect=True))
        handler = mock.Mock()
        hosted = runner.HubRunner(
            opened, registry.Registry([registry.Reactor("worker", "swarm.*.task.>", handler)]), "hub"
        )

        async def disconnect_and_recover():
            await opened._nc._process_op_err(OSError("server refused publish"))
            async with asyncio.timeout(10):
                while not opened._nc.is_connected:
                    await asyncio.sleep(0.01)
            await opened._nc.flush(timeout=2)

        try:
            opened._loop.run(disconnect_and_recover())
            opened.events.publish("swarm.s.task.claimed", event())
            assert hosted.run_once(timeout_ms=100) == {"worker": ["handled"]}
            handler.assert_called_once_with(event())
        finally:
            hosted.stop()
            opened.close()
