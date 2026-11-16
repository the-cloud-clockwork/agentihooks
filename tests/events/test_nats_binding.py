from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from nats.js.api import StreamConfig

from scripts.events import nats
from scripts.events.config import BusConfig


@pytest.mark.parametrize("provision", [False, True])
def test_bus_provisions_only_when_requested(monkeypatch, provision):
    js = SimpleNamespace(
        stream_info=AsyncMock(side_effect=nats.NotFoundError()),
        streams_info=AsyncMock(return_value=[]),
        add_stream=AsyncMock(),
        create_key_value=AsyncMock(),
        key_value=AsyncMock(),
    )
    client = SimpleNamespace(jetstream=Mock(return_value=js), close=AsyncMock(), options={})
    connect = AsyncMock(return_value=client)
    monkeypatch.setattr(nats.nats_client, "connect", connect)
    bus = nats.NatsBus(BusConfig(provision=provision), "nats://localhost:4222")
    try:
        assert bus._config.provision is provision
        assert js.add_stream.await_count == (3 if provision else 0)
        if provision:
            assert js.add_stream.await_args_list[2] == call(
                StreamConfig(subjects=["ci.lab.head.>"], duplicate_window=120.0),
                name="AGENTIHOOKS-CI-LAB-HEADS",
                num_replicas=1,
            )
        state = bus.state("hub-github")
        assert bus.state("hub-github") is state
        if provision:
            js.create_key_value.assert_awaited_once_with(bucket="hub-github", replicas=1, history=nats.KV_HISTORY)
            js.key_value.assert_not_awaited()
            assert state._kv is js.create_key_value.return_value
        else:
            js.key_value.assert_awaited_once_with("hub-github")
            js.create_key_value.assert_not_awaited()
            assert state._kv is js.key_value.return_value
    finally:
        bus.close()


def test_provisioning_remains_the_default():
    assert BusConfig().provision is True
