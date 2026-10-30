import pytest

from scripts.events import nats
from scripts.events.config import BusConfig
from scripts.events.nats_legacy import EVENTS_MAX_AGE_S, EVENTS_MAX_BYTES, LEGACY_EVENTS
from tests.events.test_nats import url as url

pytestmark = pytest.mark.unit

TIMERS = "hub-timers"


def _stream(bus: nats.NatsBus, name: str):
    return bus._loop.run(bus._js.stream_info(name)).config


def test_hub_timers_bucket_is_created_with_one_revision_and_a_byte_cap(url):
    bus = nats.open_bus(BusConfig(url=url))
    try:
        bus.state(TIMERS)
        config = _stream(bus, f"KV_{TIMERS}")
        assert config.max_msgs_per_subject == 1
        assert config.max_bytes == nats.STATE_LIMITS[TIMERS]["max_bytes"]
        assert 0 < config.max_bytes <= 1 << 30
    finally:
        bus.close()


def test_existing_hub_timers_bucket_is_updated_to_the_limits(url):
    bus = nats.open_bus(BusConfig(url=url))
    try:
        state = bus._loop.run(bus._js.create_key_value(bucket=TIMERS, history=nats.KV_HISTORY))
        bus._loop.run(state.put("deadlines", b"{}"))
        store = bus.state(TIMERS)
        config = _stream(bus, f"KV_{TIMERS}")
        assert config.max_msgs_per_subject == 1
        assert config.max_bytes == nats.STATE_LIMITS[TIMERS]["max_bytes"]
        assert store.get("deadlines").value == b"{}"
    finally:
        bus.close()


def test_other_buckets_keep_the_default_history(url):
    bus = nats.open_bus(BusConfig(url=url))
    try:
        bus.state("hub-github")
        config = _stream(bus, "KV_hub-github")
        assert config.max_msgs_per_subject == nats.KV_HISTORY
        assert config.max_bytes in (None, -1)
    finally:
        bus.close()


def test_events_stream_is_created_with_age_and_byte_limits(url):
    bus = nats.open_bus(BusConfig(url=url))
    try:
        config = _stream(bus, nats.EVENT_STREAM)
        assert config.max_age == EVENTS_MAX_AGE_S
        assert config.max_bytes == EVENTS_MAX_BYTES
        assert 0 < EVENTS_MAX_AGE_S <= 30 * 24 * 3600
        assert 0 < EVENTS_MAX_BYTES <= 4 << 30
    finally:
        bus.close()


def test_existing_unbounded_events_stream_is_updated_to_the_limits(url):
    first = nats.open_bus(BusConfig(url=url))
    try:
        first._loop.run(first._js.delete_stream(nats.EVENT_STREAM))
        first._loop.run(
            first._js.add_stream(name=nats.EVENT_STREAM, subjects=LEGACY_EVENTS, duplicate_window=12.5, max_msgs=99)
        )
    finally:
        first.close()
    second = nats.open_bus(BusConfig(url=url))
    try:
        config = _stream(second, nats.EVENT_STREAM)
        assert config.max_age == EVENTS_MAX_AGE_S
        assert config.max_bytes == EVENTS_MAX_BYTES
        assert config.max_msgs == 99
        assert config.subjects == LEGACY_EVENTS
    finally:
        second.close()
