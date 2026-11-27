"""The lab head stream on a real NATS server: its provisioning across controller restarts and the lab head log."""

import pytest
from nats.js.api import StreamInfo

from scripts.ci_lab import scheduler
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.events import factory, memory, nats
from scripts.events.bus import NoStream
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
        with pytest.raises(NoStream):
            receiver.lab_heads.publish("ci.lab.head.push", envelope(HEAD))
        assert _stream(controller, nats.EVENT_STREAM).state.messages == 0
    finally:
        receiver.close()
        controller.close()


@pytest.mark.feature("lab-head-own-stream")
def test_the_lab_head_stream_takes_the_bus_dedupe_window(url: str) -> None:
    bus = nats.open_bus(BusConfig(url=url, dedupe_window_ms=12_500))
    try:
        assert _stream(bus, nats.LAB_STREAM).config.duplicate_window == 12.5
    finally:
        bus.close()


@pytest.mark.feature("lab-head-own-stream")
def test_the_memory_bus_keeps_lab_heads_on_their_own_log_with_its_dead_letters() -> None:
    bus = memory.open_bus(BusConfig())
    assert bus.lab_heads.publish("ci.lab.head.push", envelope(HEAD))
    assert [subject for subject, _ in bus.lab_heads.entries] == ["ci.lab.head.push"]
    assert bus._legacy_events.entries == []
    assert bus.lab_heads.dead is bus.dead


def _scheduler_heads(url: str) -> list[scheduler.Head]:
    """Start the scheduler's bind mode bus, read one batch through its durable consumer, then stop it."""
    bus = factory.open_bus(BusConfig(backend="nats", url=url, provision=False))
    try:
        return scheduler.BusInbox(bus.lab_heads.consumer(scheduler.DURABLE, scheduler.SUBJECT)).heads()
    finally:
        bus.close()


@pytest.mark.feature("lab-head-durable-consumer")
def test_a_bound_scheduler_reads_the_heads_published_while_it_was_stopped(url: str) -> None:
    down = HeadEvent("push", "https://github.com/o/r", "c" * 40, "b" * 40, "dev", "pushed")
    controller = nats.open_bus(BusConfig(url=url))
    receiver = nats.open_bus(BusConfig(url=url, provision=False))
    try:
        assert receiver.lab_heads.publish("ci.lab.head.push", envelope(HEAD))
        assert [head.event for head in _scheduler_heads(url)] == [HEAD]
        assert receiver.lab_heads.publish("ci.lab.head.push", envelope(down))
        assert [head.event for head in _scheduler_heads(url)] == [down]
    finally:
        receiver.close()
        controller.close()
