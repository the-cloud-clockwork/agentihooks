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
