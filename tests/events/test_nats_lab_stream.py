"""The lab head stream on a real NATS server: its provisioning across controller restarts and the lab head log."""

from dataclasses import replace
from typing import Any

import pytest
from nats.js.api import StreamInfo
from nats.js.errors import NoStreamResponseError

from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.events import memory, nats
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.nats_legacy import LEGACY_EVENTS
from tests.events.test_nats import url as url
from tests.hive.test_nats_grant import _user_creds
from tests.hive.test_nats_grant import signer as signer

pytestmark = pytest.mark.unit

HEAD = HeadEvent("push", "https://github.com/o/r", "b" * 40, "a" * 40, "dev", "pushed")
LEGACY_EVENT_STREAM = "AGENTIHOOKS_EVENTS"


def _stream(bus: nats.NatsBus, name: str) -> StreamInfo:
    return bus._loop.run(bus._js.stream_info(name))


@pytest.mark.feature("lab-head-own-stream")
def test_provisioning_keeps_the_lab_head_subjects_across_a_controller_restart(url: str) -> None:
    seed = nats.open_bus(BusConfig(url=url, provision=False))
    seed._loop.run(seed._js.add_stream(name=LEGACY_EVENT_STREAM, subjects=[*LEGACY_EVENTS, "ci.lab.head.>"]))
    seed.close()
    controller = nats.open_bus(BusConfig(url=url))
    assert controller.lab_heads.publish("ci.lab.head.push", envelope(HEAD))
    controller.close()
    restarted = nats.open_bus(BusConfig(url=url))
    try:
        lab = _stream(restarted, "AGENTIHOOKS-CI-LAB-HEADS")
        assert lab.config.subjects == ["ci.lab.head.>"]
        assert lab.state.messages == 1
        assert _stream(restarted, LEGACY_EVENT_STREAM).config.subjects == LEGACY_EVENTS
    finally:
        restarted.close()


@pytest.mark.feature("lab-head-own-stream")
def test_the_receiver_publishes_lab_heads_to_the_lab_stream_by_name(url: str) -> None:
    controller = nats.open_bus(BusConfig(url=url))
    receiver = nats.open_bus(BusConfig(url=url, provision=False))
    try:
        assert receiver.lab_heads.publish("ci.lab.head.notice", envelope(HEAD))
        assert not receiver.lab_heads.publish("ci.lab.head.notice", envelope(HEAD))
        assert _stream(controller, nats.LAB_STREAM).state.messages == 1
        assert _stream(controller, nats.EVENT_STREAM).state.messages == 0
    finally:
        receiver.close()
        controller.close()


@pytest.mark.feature("lab-head-own-stream")
def test_a_lab_head_publish_without_the_lab_stream_fails_instead_of_falling_back(url: str) -> None:
    controller = nats.open_bus(BusConfig(url=url))
    controller._loop.run(controller._js.delete_stream(nats.LAB_STREAM))
    receiver = nats.open_bus(BusConfig(url=url, provision=False))
    try:
        with pytest.raises(NoStreamResponseError):
            receiver.lab_heads.publish("ci.lab.head.push", envelope(HEAD))
        assert _stream(controller, nats.EVENT_STREAM).state.messages == 0
    finally:
        receiver.close()
        controller.close()


@pytest.mark.feature("lab-head-own-stream")
def test_the_lab_head_log_takes_the_bus_credential_policy_dedupe_window_and_scope(signer: Any) -> None:
    credential, policy = _user_creds(signer.account, "lab"), QueuePolicy(max_deliver=2, ack_wait_ms=300)
    config = BusConfig(url=signer.url, credentials=credential, queue=policy, dedupe_window_ms=12_500)
    bus = nats.open_bus(config)
    scoped = nats.open_bus(replace(config, scoped=True))
    try:
        log = bus.lab_heads
        assert (log._credentials, log._policy, log._provision) == (credential, policy, True)
        assert _stream(bus, nats.LAB_STREAM).config.duplicate_window == 12.5
        assert scoped.for_swarm("rig").lab_heads is scoped.lab_heads
    finally:
        scoped.close()
        bus.close()


@pytest.mark.feature("lab-head-own-stream")
def test_the_memory_bus_keeps_lab_heads_on_their_own_log_with_its_dead_letters() -> None:
    bus = memory.open_bus(BusConfig())
    assert bus.lab_heads.publish("ci.lab.head.push", envelope(HEAD))
    assert [subject for subject, _ in bus.lab_heads.entries] == ["ci.lab.head.push"]
    assert bus._legacy_events.entries == []
    assert bus.lab_heads.dead is bus.dead
