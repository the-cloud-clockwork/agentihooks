from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scripts.events import memory, nats
from scripts.events.bus import StateStore
from scripts.events.config import BusConfig
from scripts.swarm.name_state import NameState, state_key
from scripts.swarm.naming import NameRegistry
from tests.events import nats_server

pytestmark = pytest.mark.integration


@pytest.mark.feature("bus-state-batch")
def test_memory_batch_preserves_order_missing_keys_and_duplicates() -> None:
    bus = memory.open_bus(BusConfig())
    try:
        store = bus.state("names")
        revision = store.create("a", b"value")
        assert isinstance(store, StateStore)
        entries = store.get_many(["missing", "a", "a"])
        assert entries == [None, store.get("a"), store.get("a")]
        assert entries[1].revision == revision
        assert store.get_many([]) == []
    finally:
        bus.close()


@pytest.mark.feature("bus-state-batch")
@pytest.mark.parametrize("scoped", [False, True])
def test_name_lookup_uses_one_bus_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scoped: bool) -> None:
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(backend="nats", url=url, scoped=scoped))
        try:
            view = bus.for_swarm("batch") if scoped else bus
            store = view.state("names")
            store.create(state_key("alias", "first"), b'"one"')
            store.create(state_key("alias", "second"), b'"two"')
            store.create(state_key("alias", "removed"), b"")
            registry = NameRegistry(None)
            registry.names_state = NameState(None, store)
            publish = AsyncMock(wraps=bus._nc.publish)
            with patch.object(bus._nc, "publish", publish):
                assert registry.resolve_many(["second", "missing", "first", "removed", "second"]) == {
                    "second": "two",
                    "missing": "missing",
                    "first": "one",
                    "removed": "removed",
                }
            requests = [call for call in publish.call_args_list if call.args[0].startswith("$JS.")]
            assert len(requests) == 1
            with patch.object(bus._nc, "publish", AsyncMock(wraps=bus._nc.publish)) as empty:
                assert store.get_many([]) == []
                empty.assert_not_called()
        finally:
            bus.close()
