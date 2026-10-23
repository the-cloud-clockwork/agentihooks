import hashlib
import json

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from scripts.hive import execution_events
from scripts.hive.spoke import Spoke, claim_key
from tests.hive.test_spoke import reading
from tests.hive.test_spoke import rig as rig

pytestmark = pytest.mark.unit


def test_spoke_claims_and_presence_stay_in_its_swarm(rig):
    bus = open_bus(BusConfig(scoped=True), clock=lambda: rig.clock.now)
    try:
        own = bus.for_swarm("rig")
        own.queue("eng", rig.config.policy).offer(rig.event)
        spoke = Spoke(rig.config, bus, rig.runtime, rig.tasks, reading)
        assert spoke.poll() == "started"
        assert own.state("spoke-work").get(claim_key(rig.event)) is not None
        assert bus.state("spoke-work").get(claim_key(rig.event)) is None
        assert own.state("hive-presence").get(rig.config.hive) is not None
        assert bus.for_swarm("other").state("spoke-work").get(claim_key(rig.event)) is None
    finally:
        bus.close()


def test_execution_markers_and_start_fences_follow_the_event_swarm(rig):
    from dataclasses import replace

    bus = open_bus(BusConfig(scoped=True))
    event = replace(rig.event, id="started", type="execution.started", data={"name": "agent", "generation": 7})
    try:
        execution_events.started(bus, event)
        own = bus.for_swarm("rig")
        key = execution_events.identity("rig", "agent", event.version)
        assert json.loads(own.state(execution_events.STARTS).get(key).value)["id"] == "started"
        assert own.state(execution_events.STATE).get(hashlib.sha256(event.id.encode()).hexdigest()).value == b"sent"
        assert bus.state(execution_events.STARTS).get(key) is None
        assert bus.for_swarm("other").state(execution_events.STARTS).get(key) is None
    finally:
        bus.close()
