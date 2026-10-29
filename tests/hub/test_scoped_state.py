import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus

pytestmark = pytest.mark.unit


def test_lifecycle_reads_the_claim_from_the_event_swarm():
    from scripts.hub.lifecycle import Lifecycle

    bus = open_bus(BusConfig(scoped=True))
    store = Mock()
    store.execution_registry.get.return_value = SimpleNamespace(
        execution_id="different", generation=0, task="task", name="agent"
    )
    lifecycle = Lifecycle(bus, store=store)
    event = Envelope(
        "exit",
        "execution.exited",
        "rig",
        "execution",
        "execution",
        1,
        0,
        "hive",
        {"claim_key": "claim", "claim_revision": 1, "task": "task", "name": "agent"},
    )
    try:
        bus.for_swarm("rig").state("spoke-work").create("claim", json.dumps({"state": "started"}).encode())
        lifecycle.handle(event)
        store.execution_registry.get.assert_called_once_with("rig", "execution")
    finally:
        bus.close()


def test_lifecycle_never_uses_a_global_claim_for_a_scoped_event():
    from scripts.hub.lifecycle import Lifecycle

    bus = open_bus(BusConfig(scoped=True))
    store = Mock()
    lifecycle = Lifecycle(bus, store=store)
    event = Envelope(
        "exit",
        "execution.exited",
        "rig",
        "execution",
        "execution",
        1,
        0,
        "hive",
        {"claim_key": "claim", "claim_revision": 1, "task": "task", "name": "agent"},
    )
    try:
        bus.state("spoke-work").create("claim", json.dumps({"state": "started"}).encode())
        lifecycle.handle(event)
        store.execution_registry.get.assert_not_called()
    finally:
        bus.close()
