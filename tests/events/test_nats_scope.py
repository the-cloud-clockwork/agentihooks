import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.events import nats_scope
from scripts.events.config import SCOPED_STATE_BUCKETS, BusConfig, QueuePolicy
from scripts.events.subjects import stream_name

pytestmark = pytest.mark.unit


class Page:
    def __init__(self, names, total):
        self.streams = [SimpleNamespace(config=SimpleNamespace(name=name)) for name in names]
        self.total = total

    def __iter__(self):
        return iter(self.streams)


def test_inventory_walks_every_page_and_ignores_unrelated_streams():
    calls = []
    pages = [Page([stream_name("z", "EVENTS")], 3), Page(["unrelated"], 3), Page([stream_name("rig", "EVENTS")], 3)]

    async def page(*, offset):
        assert offset == len(calls)
        calls.append(offset)
        return pages[offset]

    assert asyncio.run(nats_scope.discover_swarms(SimpleNamespace(streams_info_iterator=page))) == ["z", "rig"]
    assert calls == [0, 1, 2]


def test_inventory_refuses_a_page_that_cannot_advance():
    js = SimpleNamespace(streams_info_iterator=AsyncMock(return_value=Page([], 1)))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(nats_scope.discover_swarms(js))
    assert str(caught.value) == "NATS stream inventory did not advance"


def test_scope_provisions_dedupe_and_buckets_then_reuses_owned_resources(monkeypatch):
    store = Mock(side_effect=lambda *args: SimpleNamespace(args=args))
    queues = Mock(side_effect=lambda loop, js, name, policy, config, swarm: SimpleNamespace(policy=policy, name=name))
    monkeypatch.setattr(nats_scope, "ScopedStateStore", store)
    monkeypatch.setattr(nats_scope, "NatsWorkQueue", queues)
    config = BusConfig(scoped=True, dedupe_window_ms=12345)
    root = SimpleNamespace(
        _config=config, _loop=SimpleNamespace(run=asyncio.run), _js=SimpleNamespace(add_stream=AsyncMock())
    )
    view = nats_scope.NatsScopedBus(root, "rig")
    assert view.events._log._swarm == "rig"
    assert view.dead._log._swarm == "rig"
    assert [call.kwargs for call in root._js.add_stream.await_args_list] == [
        {"name": stream_name("rig", "EVENTS"), "subjects": ["swarm.rig.events.>"], "duplicate_window": 12.345},
        {"name": stream_name("rig", "DEAD"), "subjects": ["swarm.rig.dead.>"], "duplicate_window": 12.345},
    ]
    assert [call.args for call in store.call_args_list] == [(root, "rig", bucket) for bucket in SCOPED_STATE_BUCKETS]
    assert view.state("custom") is view.state("custom")
    policy = QueuePolicy(max_deliver=2)
    queue = view.queue("eng", policy)
    assert view.queue("swarm.rig.ready.eng", policy) is queue
    queues.assert_called_once_with(root._loop, root._js, "swarm.rig.ready.eng", policy, config, "rig")
    with pytest.raises(ValueError) as caught:
        view.queue("eng", QueuePolicy())
    assert str(caught.value) == "work queue 'eng' already has another policy"
    assert view.for_swarm("rig") is view
    with pytest.raises(ValueError) as caught:
        view.for_swarm("other")
    assert str(caught.value) == "a scoped bus cannot change its swarm"


def test_reopening_scoped_queue_preserves_the_existing_stream_configuration(monkeypatch):
    from scripts.events import nats
    from scripts.events.config import QueuePolicy

    js = SimpleNamespace(stream_info=AsyncMock(return_value=SimpleNamespace()), add_stream=AsyncMock())
    pull = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(nats, "_pull", pull)
    loop = SimpleNamespace(run=asyncio.run)
    policy = QueuePolicy(max_deliver=2)
    config = BusConfig(scoped=True)
    queue = nats.NatsWorkQueue(loop, js, "swarm.rig.ready.eng", policy, config, "rig")
    js.stream_info.assert_awaited_once_with(stream_name("rig", "QUEUE_eng"))
    js.add_stream.assert_not_awaited()
    assert queue._subject == "swarm.rig.queue.eng.work"
    assert queue._filter == "swarm.rig.queue.eng.>"
