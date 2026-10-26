import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from nats.js.errors import NotFoundError

from scripts.events import nats, nats_scope
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.resource_names import consumer_name, legacy_queue_stream


def loop():
    return SimpleNamespace(run=asyncio.run, close=Mock(), closed=False)


def test_legacy_event_filter_and_dead_attempt_cap_remain_usable():
    policy = QueuePolicy(max_deliver=4)
    js = SimpleNamespace(
        consumer_info=AsyncMock(
            return_value=SimpleNamespace(config=SimpleNamespace(filter_subjects=None, filter_subject="swarm.>"))
        ),
        add_consumer=AsyncMock(),
        pull_subscribe_bind=AsyncMock(),
    )
    asyncio.run(nats._pull(js, "AGENTIHOOKS_EVENTS", "reader", "swarm.>", policy, False))
    js.pull_subscribe_bind.assert_awaited_once_with(durable="reader", stream="AGENTIHOOKS_EVENTS")
    js.consumer_info.side_effect = NotFoundError()
    asyncio.run(nats._pull(js, "AGENTIHOOKS_DEAD", "reader", "dead.>", policy, True))
    assert js.add_consumer.await_args.args[0] == "AGENTIHOOKS_DEAD"
    assert js.add_consumer.await_args.kwargs["config"].max_deliver == 4


def test_event_log_keeps_the_legacy_durable_and_credentials(monkeypatch):
    permissions = {"allow": ["legacy.swarm.>"]}
    monkeypatch.setattr(nats.nkey, "parse_creds", Mock(return_value=("jwt", "key")))
    monkeypatch.setattr(nats.nkey, "claims", Mock(return_value={"nats": {"pub": permissions}}))
    pull = AsyncMock(return_value="subscription")
    consumer = Mock()
    publish = AsyncMock(return_value=True)
    monkeypatch.setattr(nats, "_pull", pull)
    monkeypatch.setattr(nats, "NatsConsumer", consumer)
    monkeypatch.setattr(nats, "_publish", publish)
    js, owned_loop, policy = object(), loop(), QueuePolicy()
    log = nats.NatsEventLog(owned_loop, js, policy, "AGENTIHOOKS_EVENTS", False, credentials="old-creds")
    log.consumer("reader_old", "swarm.>")
    pull.assert_awaited_once_with(
        js, "AGENTIHOOKS_EVENTS", consumer_name("reader_old"), "swarm.>", policy, False, "reader_old"
    )
    consumer.assert_called_once_with(owned_loop, "subscription", js, policy, "events.reader_old", None, "old-creds")
    event = object()
    assert log.publish("swarm.rig.task.ready", event)
    publish.assert_awaited_once_with(js, "legacy.swarm.rig.task.ready", "AGENTIHOOKS_EVENTS", event)


@pytest.mark.parametrize(("provision", "swarm"), [(False, None), (True, None), (False, "rig"), (True, "rig")])
def test_queue_binding_preserves_identity_credentials_and_lookup_capabilities(monkeypatch, provision, swarm):
    js = SimpleNamespace(stream_info=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(metadata={}))))
    config, policy, owned_loop = (
        BusConfig(provision=provision, scoped=swarm is not None, credentials="member-creds"),
        QueuePolicy(),
        loop(),
    )
    name = "swarm.rig.ready.eng" if swarm else "work_old"
    names = nats.queue_names(name, swarm)
    legacy = legacy_queue_stream(name) if swarm is None else "AGENTIHOOKS_726967_QUEUE_eng"
    binding, stream, pull = (
        AsyncMock(return_value=legacy),
        AsyncMock(return_value=legacy),
        AsyncMock(return_value="subscription"),
    )
    provisioner, consumer = AsyncMock(), Mock()
    monkeypatch.setattr(nats, "queue_binding", binding)
    monkeypatch.setattr(nats, "existing_stream", stream)
    monkeypatch.setattr(nats, "_pull", pull)
    monkeypatch.setattr(nats, "provision_queue", provisioner)
    monkeypatch.setattr(nats, "NatsConsumer", consumer)
    queue = nats.NatsWorkQueue(owned_loop, js, name, policy, config, swarm)
    legacy_durable = "work_old" if swarm is None else "eng"
    if not provision and swarm is None:
        binding.assert_awaited_once_with(js, names.stream, legacy, names.durable, legacy_durable)
        stream.assert_not_awaited()
    else:
        stream.assert_awaited_once_with(js, names.stream, legacy)
        binding.assert_not_awaited()
    pull.assert_awaited_once_with(js, legacy, names.durable, names.filter, policy, provision, legacy_durable)
    consumer.assert_called_once_with(owned_loop, "subscription", js, policy, f"queue.{name}", swarm, "member-creds")
    assert (queue._stream, queue._subject, queue._filter) == (legacy, names.subject, names.filter)


def test_scoped_logs_receive_the_root_credential_context(monkeypatch):
    config, js, owned_loop = BusConfig(provision=False, scoped=True, credentials="member-creds"), object(), loop()
    root = SimpleNamespace(_config=config, _js=js, _loop=owned_loop)
    select = AsyncMock(side_effect=["events-stream", "dead-stream"])
    logs = Mock()
    monkeypatch.setattr(nats_scope, "existing_stream", select)
    monkeypatch.setattr(nats_scope, "NatsEventLog", logs)
    nats_scope.NatsScopedBus(root, "rig")
    assert logs.call_args_list == [
        call(owned_loop, js, config.queue, "events-stream", False, "rig", "member-creds"),
        call(owned_loop, js, config.queue, "dead-stream", False, "rig", "member-creds"),
    ]


@pytest.mark.parametrize("scoped", [False, True])
def test_root_binding_discovers_global_streams_only_for_unscoped_clients(monkeypatch, scoped):
    owned_loop = loop()
    js = object()
    client = SimpleNamespace(jetstream=Mock(return_value=js))
    config = BusConfig(provision=False, scoped=scoped, credentials="member-creds")
    selector = AsyncMock(side_effect=["old-events", "old-dead"])
    monkeypatch.setattr(nats, "_Loop", lambda: owned_loop)
    monkeypatch.setattr(nats.nats_client, "connect", AsyncMock(return_value=client))
    logs = Mock()
    monkeypatch.setattr(nats, "NatsEventLog", logs)
    monkeypatch.setattr(nats, "auth_options", Mock(return_value={}))
    monkeypatch.setattr(nats, "root_stream", selector)
    nats.NatsBus(config, "nats://example:4222")
    assert [entry.kwargs for entry in logs.call_args_list] == [
        {"credentials": "member-creds"},
        {"credentials": "member-creds"},
    ]
    assert selector.await_args_list == (
        []
        if scoped
        else [
            call(js, nats.EVENT_STREAM, "AGENTIHOOKS_EVENTS", "member-creds"),
            call(js, nats.DEAD_STREAM, "AGENTIHOOKS_DEAD", "member-creds"),
        ]
    )
