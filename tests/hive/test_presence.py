import json

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from scripts.hive import presence


@pytest.fixture
def bus():
    opened = open_bus(BusConfig())
    yield opened
    opened.close()


def test_presence_uses_a_millisecond_wall_clock(monkeypatch):
    monkeypatch.setattr(presence.time, "time_ns", lambda: 1_234_567_890)
    assert presence.now_ms() == 1234


def test_heartbeat_creates_then_renews_the_same_hive_by_revision(bus):
    first = presence.beat(bus, "hive-a", 1234)
    store = bus.state("hive-presence")
    assert first == store.get("hive-a").revision
    assert json.loads(store.get("hive-a").value) == {"heartbeat_ms": 1234, "expires_ms": 46234}
    second = presence.beat(bus, "hive-a", 5678)
    assert second > first
    assert second == store.get("hive-a").revision
    assert json.loads(store.get("hive-a").value) == {"heartbeat_ms": 5678, "expires_ms": 50678}
    assert store.get("hive-b") is None


@pytest.mark.parametrize("at_ms, expected", [(9, False), (10, True), (11, True)])
def test_a_known_hive_is_lost_at_its_expiry_boundary(bus, at_ms, expected):
    bus.state("hive-presence").create("hive-a", b'{"heartbeat_ms":0,"expires_ms":10}')
    assert presence.lost(bus, "hive-a", at_ms) is expected


@pytest.mark.parametrize("hive", [None, "", "unknown"])
def test_missing_legacy_presence_does_not_revoke_an_owner(bus, hive):
    assert presence.lost(bus, hive, 100000) is False
