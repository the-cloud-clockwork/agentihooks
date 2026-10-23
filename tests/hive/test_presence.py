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


def test_empty_owner_does_not_read_the_state_backend():
    from unittest.mock import Mock

    bus = Mock()
    assert presence.lost(bus, None, 10) is False
    assert presence.lost(bus, "", 10) is False
    bus.state.assert_not_called()


def test_heartbeat_context_starts_and_joins_its_worker(bus, monkeypatch):
    from unittest.mock import Mock

    interval = Mock()
    factory = Mock(return_value=interval)
    monkeypatch.setattr(presence.process_interval, "Interval", factory)
    heartbeat = presence.Heartbeat(bus, "hive-a", lambda: 100)
    factory.assert_called_once_with(heartbeat._renew, every=15)
    assert heartbeat.__enter__() is heartbeat
    interval.start.assert_called_once_with()
    assert json.loads(bus.state("hive-presence").get("hive-a").value) == {
        "heartbeat_ms": 100,
        "expires_ms": 45100,
    }
    assert heartbeat.__exit__(None, None, None) is None
    interval.close.assert_called_once_with()


def test_renewal_uses_the_same_hive_clock_and_interval(bus, monkeypatch):
    from unittest.mock import Mock, call

    interval = Mock()
    monkeypatch.setattr(presence.process_interval, "Interval", Mock(return_value=interval))
    heartbeat = presence.Heartbeat(bus, "hive-a", Mock(side_effect=[100, 200]))
    write = Mock()
    monkeypatch.setattr(presence, "beat", write)
    heartbeat._renew()
    heartbeat._renew()
    assert write.call_args_list == [call(bus, "hive-a", 100), call(bus, "hive-a", 200)]
    assert heartbeat.check() is None
    interval.check.assert_called_once_with()


def test_renewal_errors_are_raised_by_the_health_check_and_context_exit(bus, monkeypatch):
    from unittest.mock import Mock

    failure = RuntimeError("presence unavailable")
    interval = Mock()
    interval.check.side_effect = failure
    interval.close.side_effect = failure
    monkeypatch.setattr(presence.process_interval, "Interval", Mock(return_value=interval))
    heartbeat = presence.Heartbeat(bus, "hive-a", lambda: 100)
    with pytest.raises(RuntimeError) as raised:
        heartbeat.check()
    assert raised.value is failure
    with pytest.raises(RuntimeError) as raised:
        heartbeat.__exit__(None, None, None)
    assert raised.value is failure
    interval.check.assert_called_once_with()
    interval.close.assert_called_once_with()


def test_context_refuses_a_worker_that_did_not_stop(bus, monkeypatch):
    from unittest.mock import Mock

    interval = Mock()
    interval.close.side_effect = RuntimeError("scheduled interval did not stop")
    monkeypatch.setattr(presence.process_interval, "Interval", Mock(return_value=interval))
    heartbeat = presence.Heartbeat(bus, "hive-a", lambda: 100)
    with pytest.raises(RuntimeError) as raised:
        heartbeat.__exit__(None, None, None)
    assert str(raised.value) == "scheduled interval did not stop"
    interval.close.assert_called_once_with()


@pytest.mark.parametrize("fail", [False, True])
def test_real_heartbeat_worker_stops_on_normal_and_exceptional_exit(bus, fail):
    heartbeat = presence.Heartbeat(bus, "hive-a", lambda: 100)
    try:
        with heartbeat:
            assert heartbeat._interval._worker.is_alive()
            if fail:
                raise ValueError("body failed")
    except ValueError as error:
        assert fail
        assert str(error) == "body failed"
    assert not heartbeat._interval._worker.is_alive()
