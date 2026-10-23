import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from scripts.events import nats
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from tests.events.test_nats import url as url

pytestmark = pytest.mark.unit


def test_legacy_publishers_keep_both_subjects_during_concurrent_first_use(url, monkeypatch):
    first = nats.open_bus(BusConfig(url=url))
    second = nats.open_bus(BusConfig(url=url))
    barrier = threading.Barrier(2)

    def hold_snapshot(original):
        async def info(name):
            found = await original(name)
            if name == nats.EVENT_STREAM:
                await asyncio.to_thread(barrier.wait, 5)
            return found

        return info

    for bus in (first, second):
        monkeypatch.setattr(bus._js, "stream_info", hold_snapshot(bus._js.stream_info))
    event = Envelope("first", "task.changed", "rig", "task", "work", 1, 0, "hub", {})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            published = [
                pool.submit(first.events.publish, "swarm.rig.task.first", event),
                pool.submit(second.events.publish, "swarm.rig.task.second", replace(event, id="second")),
            ]
            assert [result.result(10) for result in published] == [True, True]
        assert first.events.publish("swarm.rig.task.first", replace(event, id="first-again"))
        assert second.events.publish("swarm.rig.task.second", replace(event, id="second-again"))
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize(
    "permissions,expected",
    [
        ({}, "legacy.swarm.rig.execution.started"),
        ({"allow": ["swarm.*.execution.>"]}, "swarm.rig.execution.started"),
        ({"allow": [">"], "deny": ["legacy.>"]}, "swarm.rig.execution.started"),
        ({"allow": ["legacy.swarm.rig.>"]}, "legacy.swarm.rig.execution.started"),
    ],
)
def test_legacy_wire_selection_preserves_the_signed_publish_limits(permissions, expected):
    from scripts.events.nats_legacy import legacy_target

    assert legacy_target("swarm.rig.execution.started", permissions) == expected


def test_legacy_filters_keep_existing_cursor_subjects_and_new_wire_subjects():
    from scripts.events.nats_legacy import legacy_filters, legacy_target

    assert legacy_filters("swarm.rig.>") == ["swarm.rig.>", "legacy.swarm.rig.>"]
    assert legacy_filters(">") == [">"]
    assert legacy_target("agentihooks.queue.work", {}) == "agentihooks.queue.work"
