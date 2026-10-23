import hashlib
import json

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from tests.hive.test_nats_grant import admin as admin
from tests.hive.test_nats_grant import signer as signer
from tests.hive.test_spoke import live_readiness as live_readiness
from tests.hive.test_spoke import reading
from tests.hive.test_spoke import rig as rig

pytestmark = pytest.mark.unit


def test_spoke_claims_and_presence_stay_in_its_swarm(rig):
    from scripts.hive.spoke import Spoke, claim_key

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

    from scripts.hive import execution_events

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


def test_scoped_join_grant_enables_the_scoped_client_layout(admin, signer):
    from dataclasses import replace

    from scripts.hive import auth
    from tests.hive.test_nats_grant import PUBLIC, _refresh

    member = auth.mint(admin, "hive", PUBLIC)
    configured = replace(signer, member_swarms={member["id"]: ("rig",)})
    grant = _refresh(admin, configured, member["ledger_credential"])
    assert grant["bus_scoped"] is True
    assert grant["bus_swarms"] == ["rig"]
    text = auth.env_text(
        "https://hive",
        {**grant, "ledger_credential": member["ledger_credential"], "bus_credentials": "/member/bus.creds"},
    )
    assert "AGENTIHOOKS_BUS_SCOPED=1" in text.splitlines()
    assert "AGENTIHOOKS_BUS_SWARMS=rig" in text.splitlines()
    assert "AGENTIHOOKS_BUS_CREDENTIALS=/member/bus.creds" in text.splitlines()
