import sys
import types

import pytest
from scripts.events.config import BusConfig

from scripts.events import factory

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_backend(monkeypatch):
    module = types.ModuleType("fake_bus_backend")
    module.open_bus = lambda config: ("opened", config)
    monkeypatch.setitem(sys.modules, "fake_bus_backend", module)
    return module


def test_registry_names_the_memory_and_nats_backends():
    assert factory.BACKENDS == {
        "memory": "scripts.events.memory:open_bus",
        "nats": "scripts.events.nats:open_bus",
    }


def test_open_bus_picks_the_backend_named_by_the_bus_setting(fake_backend):
    config = BusConfig.from_environ({"AGENTIHOOKS_BUS": "fake"})
    assert factory.open_bus(config, {"fake": "fake_bus_backend:open_bus"}) == ("opened", config)


def test_open_bus_imports_only_the_chosen_backend(fake_backend):
    registry = {"fake": "fake_bus_backend:open_bus", "other": "no_such_bus_backend_module:open_bus"}
    factory.open_bus(BusConfig(backend="fake"), registry)
    assert "no_such_bus_backend_module" not in sys.modules


def test_open_bus_defaults_to_the_shipped_registry(monkeypatch, fake_backend):
    monkeypatch.setitem(factory.BACKENDS, "fake", "fake_bus_backend:open_bus")
    config = BusConfig(backend="fake")
    assert factory.open_bus(config) == ("opened", config)


def test_open_bus_refuses_an_unknown_backend():
    with pytest.raises(ValueError, match=r"^unknown bus backend 'kafka'; known: memory, nats$"):
        factory.open_bus(BusConfig(backend="kafka"), {"nats": "x:y", "memory": "x:y"})
