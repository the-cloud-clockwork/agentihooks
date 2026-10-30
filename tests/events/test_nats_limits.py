import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js.api import StreamConfig
from nats.js.errors import APIError

from scripts.events import nats
from scripts.events.config import BusConfig
from scripts.events.nats_legacy import EVENTS_MAX_AGE_S, EVENTS_MAX_BYTES, LEGACY_EVENTS
from tests.events.test_nats import url as url

pytestmark = pytest.mark.unit

TIMERS = "hub-timers"


def _stream(bus: nats.NatsBus, name: str) -> StreamConfig:
    return bus._loop.run(bus._js.stream_info(name)).config


def test_hub_timers_bucket_is_created_with_one_revision_and_a_byte_cap(url: str) -> None:
    bus = nats.open_bus(BusConfig(url=url))
    try:
        bus.state(TIMERS)
        config = _stream(bus, f"KV_{TIMERS}")
        assert config.max_msgs_per_subject == 1
        assert config.max_bytes == nats.STATE_LIMITS[TIMERS]["max_bytes"]
        assert 0 < config.max_bytes <= 1 << 30
    finally:
        bus.close()


def test_existing_hub_timers_bucket_is_updated_to_the_limits(url: str) -> None:
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


def test_other_buckets_keep_the_default_history(url: str) -> None:
    bus = nats.open_bus(BusConfig(url=url))
    try:
        bus.state("hub-github")
        config = _stream(bus, "KV_hub-github")
        assert config.max_msgs_per_subject == nats.KV_HISTORY
        assert config.max_bytes in (None, -1)
    finally:
        bus.close()


def test_events_stream_is_created_with_age_and_byte_limits(url: str) -> None:
    bus = nats.open_bus(BusConfig(url=url))
    try:
        config = _stream(bus, nats.EVENT_STREAM)
        assert config.max_age == EVENTS_MAX_AGE_S
        assert config.max_bytes == EVENTS_MAX_BYTES
        assert 0 < EVENTS_MAX_AGE_S <= 30 * 24 * 3600
        assert 0 < EVENTS_MAX_BYTES <= 4 << 30
    finally:
        bus.close()


def test_existing_unbounded_events_stream_is_updated_to_the_limits(url: str) -> None:
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


def _limited_js(error_code: int) -> SimpleNamespace:
    config = StreamConfig(name=f"KV_{TIMERS}", subjects=[f"$KV.{TIMERS}.>"], max_msgs_per_subject=64, max_msgs=7)
    return SimpleNamespace(
        create_key_value=AsyncMock(side_effect=APIError(err_code=error_code)),
        stream_info=AsyncMock(return_value=SimpleNamespace(config=config)),
        update_stream=AsyncMock(),
        key_value=AsyncMock(),
    )


def test_bounded_bucket_conflict_updates_only_the_limits() -> None:
    js = _limited_js(10058)
    found = asyncio.run(nats.provision_state(js, TIMERS))
    js.create_key_value.assert_awaited_once_with(bucket=TIMERS, **nats.STATE_LIMITS[TIMERS])
    js.stream_info.assert_awaited_once_with(f"KV_{TIMERS}")
    updated = js.update_stream.await_args.kwargs["config"]
    assert updated.max_msgs_per_subject == 1
    assert updated.max_bytes == nats.STATE_LIMITS[TIMERS]["max_bytes"]
    assert updated.max_msgs == 7
    assert updated.subjects == [f"$KV.{TIMERS}.>"]
    js.key_value.assert_awaited_once_with(TIMERS)
    assert found is js.key_value.return_value


@pytest.mark.parametrize("bucket,code", [(TIMERS, 10059), ("hub-github", 10058)])
def test_other_provisioning_errors_are_raised(bucket: str, code: int) -> None:
    js = _limited_js(code)
    with pytest.raises(APIError) as caught:
        asyncio.run(nats.provision_state(js, bucket))
    assert caught.value.err_code == code
    js.update_stream.assert_not_awaited()
    js.key_value.assert_not_awaited()
