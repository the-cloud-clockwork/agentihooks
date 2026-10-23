import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js import api
from nats.js.errors import NotFoundError

from scripts.events import nats
from scripts.events.config import BusConfig
from scripts.events.subjects import stream_name

pytestmark = pytest.mark.unit


def test_legacy_queue_names_share_one_subject_for_publish_and_filter():
    names = nats.queue_names("swarm.rig.ready.eng", None)
    assert names == nats.QueueNames(
        stream=nats.queue_stream("swarm.rig.ready.eng"),
        subject="agentihooks.queue.swarm.rig.ready.eng",
        filter="agentihooks.queue.swarm.rig.ready.eng",
        durable=nats.queue_stream("swarm.rig.ready.eng").removeprefix("AGENTIHOOKS_QUEUE_"),
    )


def test_scoped_queue_names_publish_to_work_and_filter_the_lane():
    names = nats.queue_names("swarm.rig.ready.eng", "rig")
    assert names == nats.QueueNames(
        stream=stream_name("rig", "QUEUE_eng"),
        subject="swarm.rig.queue.eng.work",
        filter="swarm.rig.queue.eng.>",
        durable="eng",
    )


@pytest.mark.parametrize("metadata", [None, {}, {"agentihooks_import_pending": ""}, {"other": "1"}])
def test_migration_guard_passes_a_stream_with_no_pending_import(metadata):
    nats.require_migrated(SimpleNamespace(config=SimpleNamespace(metadata=metadata)))


def test_migration_guard_refuses_a_stream_with_a_pending_import():
    info = SimpleNamespace(config=SimpleNamespace(metadata={"agentihooks_import_pending": "1"}))
    with pytest.raises(RuntimeError) as caught:
        nats.require_migrated(info)
    assert str(caught.value) == "scoped queue migration is incomplete"


def _js(stream_info):
    return SimpleNamespace(stream_info=stream_info, add_stream=AsyncMock())


def _assert_added(js, names):
    js.add_stream.assert_awaited_once_with(
        name=names.stream,
        subjects=[names.filter],
        retention=api.RetentionPolicy.WORK_QUEUE,
        duplicate_window=12.345,
    )


def test_legacy_provisioning_adds_the_stream_without_looking_it_up():
    js = _js(AsyncMock())
    names = nats.queue_names("eng", None)
    asyncio.run(nats.provision_queue(js, names, BusConfig(dedupe_window_ms=12345), scoped=False))
    js.stream_info.assert_not_awaited()
    _assert_added(js, names)


def test_scoped_provisioning_keeps_an_existing_stream():
    js = _js(AsyncMock(return_value=SimpleNamespace()))
    names = nats.queue_names("eng", "rig")
    asyncio.run(nats.provision_queue(js, names, BusConfig(dedupe_window_ms=12345), scoped=True))
    js.stream_info.assert_awaited_once_with(names.stream)
    js.add_stream.assert_not_awaited()


def test_scoped_provisioning_adds_a_missing_stream():
    js = _js(AsyncMock(side_effect=NotFoundError()))
    names = nats.queue_names("eng", "rig")
    asyncio.run(nats.provision_queue(js, names, BusConfig(dedupe_window_ms=12345), scoped=True))
    js.stream_info.assert_awaited_once_with(names.stream)
    _assert_added(js, names)
