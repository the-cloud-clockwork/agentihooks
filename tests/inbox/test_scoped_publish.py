from unittest.mock import Mock

import fakeredis
import pytest

from scripts.events import memory
from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.hive.nats_grant import Signer
from scripts.inbox import bus
from scripts.inbox.store import InboxStore
from tests.events.test_scoped_nats import hub as hub
from tests.events.test_scoped_nats import member
from tests.hive.test_nats_grant import signer as signer


@pytest.mark.feature("scoped-member-inbox-publish")
@pytest.mark.parametrize("fails", [False, True])
def test_scoped_member_send_selects_swarm_before_state_and_only_falls_back_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fails: bool
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv("AGENTIHOOKS_BUS_SCOPED", "1")
    monkeypatch.setenv("AGENTIHOOKS_BUS_SWARMS", "swarm")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "member")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_REDIS_URL", "redis://member")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "remote")
    monkeypatch.setattr(bus, "OPEN_WAIT_S", 30.0)
    shared = memory.open_bus(BusConfig(scoped=True))
    publisher = bus.SeatPublisher(lambda: shared)
    monkeypatch.setattr(bus, "_publisher", lambda config: publisher)
    own = shared.for_swarm("swarm")
    original_publish = own.events.publish
    publish = Mock(side_effect=OSError("bus unavailable") if fails else original_publish)
    monkeypatch.setattr(own.events, "publish", publish)
    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    assert store.world == "member"
    try:
        item = store.send("alice", "eng-1@swarm", "work")
        assert store.get(item.id) == item
        if fails:
            assert "bus unavailable" in capsys.readouterr().err
            assert publisher._bus is None
        else:
            assert capsys.readouterr().err == ""
            assert publisher._bus is shared
            publish.assert_called()
            [receipt] = bus.SeatInbox(shared, "swarm").consumer("eng-1@swarm", "session").fetch(5, 0)
            assert receipt.event.id == item.id
            assert receipt.event.swarm == "swarm"
            assert receipt.event.data["text"] == "work"
    finally:
        shared.close()


@pytest.mark.feature("scoped-member-inbox-publish")
def test_scoped_nats_member_sends_without_root_state_or_redis_fallback(
    hub: Bus, signer: Signer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv("AGENTIHOOKS_BUS_SCOPED", "1")
    monkeypatch.setenv("AGENTIHOOKS_BUS_SWARMS", "rig")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "m1")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_REDIS_URL", "redis://member")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "remote")
    monkeypatch.setattr(bus, "OPEN_WAIT_S", 30.0)
    address = "eng-1@rig"
    consumer = bus.SeatInbox(hub, "rig").consumer(address, "session")
    client = member(signer)
    publisher = bus.SeatPublisher(lambda: client)
    monkeypatch.setattr(bus, "_publisher", lambda config: publisher)
    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    assert store.world == "m1"
    try:
        item = store.send("alice", address, "work")
        assert capsys.readouterr().err == ""
        assert publisher._bus is client
        [receipt] = consumer.fetch(1, 1000)
        assert receipt.event.id == item.id
        assert receipt.event.swarm == "rig"
        assert receipt.event.data["text"] == "work"
        assert store.get(item.id) == item
    finally:
        client.close()
