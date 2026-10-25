import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

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


def test_legacy_stream_creation_keeps_the_dedupe_window():
    import asyncio
    from unittest.mock import AsyncMock

    from scripts.events.nats_legacy import LEGACY_EVENTS, provision_events

    js = SimpleNamespace(add_stream=AsyncMock())
    asyncio.run(provision_events(js, "events", 12.5))
    js.add_stream.assert_awaited_once_with(name="events", subjects=LEGACY_EVENTS, duplicate_window=12.5)


def test_legacy_stream_upgrade_retains_configuration_and_refuses_unexpected_errors():
    import asyncio
    from unittest.mock import AsyncMock

    from nats.js.api import StreamConfig
    from nats.js.errors import APIError

    from scripts.events.nats_legacy import LEGACY_EVENTS, provision_events

    config = StreamConfig(name="events", subjects=["swarm.>"], max_msgs=123, duplicate_window=12.5)
    js = SimpleNamespace(
        add_stream=AsyncMock(side_effect=APIError(err_code=10058)),
        stream_info=AsyncMock(return_value=SimpleNamespace(config=config)),
        update_stream=AsyncMock(),
    )
    asyncio.run(provision_events(js, "events", 12.5))
    js.stream_info.assert_awaited_once_with("events")
    updated = js.update_stream.await_args.kwargs["config"]
    assert updated.name == "events"
    assert updated.subjects == LEGACY_EVENTS
    assert updated.max_msgs == 123
    assert updated.duplicate_window == 12.5
    for code in [10058, 10059]:
        error = APIError(err_code=code)
        js.add_stream.side_effect = error
        js.stream_info.return_value = SimpleNamespace(config=StreamConfig(name="events", subjects=LEGACY_EVENTS))
        with pytest.raises(APIError) as caught:
            asyncio.run(provision_events(js, "events", 12.5))
        assert caught.value is error


def test_missing_publish_allow_list_means_unrestricted_legacy_routing():
    from scripts.events.nats_legacy import legacy_target

    assert legacy_target("swarm.rig.execution.started", {}) == "legacy.swarm.rig.execution.started"


def test_unrestricted_signed_event_log_accepts_an_absent_publish_claim(monkeypatch):
    from unittest.mock import AsyncMock

    from scripts.events.config import QueuePolicy

    monkeypatch.setattr(nats.nkey, "parse_creds", lambda _: ("synthetic", "synthetic"))
    monkeypatch.setattr(nats.nkey, "claims", lambda _: {"nats": {}})
    js = SimpleNamespace(
        _nc=SimpleNamespace(max_payload=1048576),
        publish=AsyncMock(return_value=SimpleNamespace(duplicate=False)),
    )
    log = nats.NatsEventLog(
        SimpleNamespace(run=asyncio.run), js, QueuePolicy(), nats.EVENT_STREAM, True, credentials="synthetic"
    )
    event = Envelope("unrestricted", "ready", "rig", "task", "work", 1, 0, "author", {})
    assert log.publish("swarm.rig.ready", event)
    assert js.publish.await_args.args[0] == "legacy.swarm.rig.ready"
