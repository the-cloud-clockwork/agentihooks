"""The lab head stream on a real NATS server: its provisioning across controller restarts and the lab head log."""

import pytest
from nats.js.api import StreamInfo
from nats.js.errors import NoStreamResponseError

from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.events import nats
from scripts.events.config import BusConfig
from scripts.events.nats_legacy import LEGACY_EVENTS
from tests.events.test_nats import url as url

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
