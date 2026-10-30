import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from nats.js.api import StreamConfig
from nats.js.errors import APIError

from scripts.events import nats, nats_payload
from scripts.events.bus import StateEntry
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


def _chunk_bytes(bus: nats.NatsBus) -> int:
    stream = f"KV_{TIMERS}"
    info = bus._loop.run(bus._js.stream_info(stream, subjects_filter=f"$KV.{TIMERS}._chunks.>"))
    return sum(len(bus._loop.run(bus._js.get_last_msg(stream, name)).data) for name in info.state.subjects or {})


def test_over_cap_bucket_sheds_stale_chunks_before_the_cap_and_keeps_writing(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nats, "STATE_LIMITS", {})
    old = nats.open_bus(BusConfig(url=url))
    try:
        size = old._nc.max_payload - nats_payload.KV_HEADER_BUDGET + 1
        values = [random.Random(seed).randbytes(size) for seed in range(9)]
        state = old.state(TIMERS)
        revision = state.create("deadlines", values[0])
        single = _chunk_bytes(old)
        for value in values[1:-1]:
            revision = state.update("deadlines", value, revision)
        assert _chunk_bytes(old) == 8 * single
    finally:
        old.close()
    cap = 3 * (size + nats_payload.KV_HEADER_BUDGET)
    monkeypatch.setattr(nats, "STATE_LIMITS", {TIMERS: {"history": 1, "max_bytes": cap}})
    new = nats.open_bus(BusConfig(url=url))
    try:
        store = new.state(TIMERS)
        config = _stream(new, f"KV_{TIMERS}")
        assert (config.max_msgs_per_subject, config.max_bytes) == (1, cap)
        assert _chunk_bytes(new) == single
        assert store.get("deadlines").value == values[-2]
        latest = store.update("deadlines", values[-1], store.get("deadlines").revision)
        assert store.get("deadlines") == StateEntry("deadlines", values[-1], latest)
        assert _chunk_bytes(new) == single
    finally:
        new.close()


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
        stream_info=AsyncMock(return_value=SimpleNamespace(config=config, state=SimpleNamespace(subjects={}))),
        update_stream=AsyncMock(),
        key_value=AsyncMock(),
    )


def test_bounded_bucket_conflict_updates_only_the_limits() -> None:
    js = _limited_js(10058)
    found = asyncio.run(nats.provision_state(js, TIMERS))
    js.create_key_value.assert_awaited_once_with(bucket=TIMERS, **nats.STATE_LIMITS[TIMERS])
    assert js.stream_info.await_args_list == [
        call(f"KV_{TIMERS}"),
        call(f"KV_{TIMERS}", subjects_filter=f"$KV.{TIMERS}.>"),
    ]
    trimmed, updated = (update.kwargs["config"] for update in js.update_stream.await_args_list)
    assert trimmed.max_msgs_per_subject == 1
    assert trimmed.max_bytes is None
    assert trimmed.max_msgs == 7
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


def test_reclaim_all_keeps_only_chunks_of_each_current_value() -> None:
    prefix = "$KV.b."

    def token(key: str, base: int) -> str:
        return f"{nats_payload._owner(key)}.{base}.{'a' * 31}{base}"

    live, stale = token("deadlines", 4), token("deadlines", 2)
    staged, orphan = token("neighbour", 9), token("neighbour", 2)
    names = [
        f"{prefix}deadlines",
        f"{prefix}_chunks.{live}.0",
        f"{prefix}_chunks.{stale}.0",
        f"{prefix}_chunks.{stale}.1",
        f"{prefix}neighbour",
        f"{prefix}_chunks.{staged}.0",
        f"{prefix}_chunks.{orphan}.0",
    ]
    values = {
        f"{prefix}deadlines": SimpleNamespace(data=nats_payload.PAGED + f'["{live}", 1]'.encode(), seq=5),
        f"{prefix}neighbour": SimpleNamespace(data=None, seq=9),
    }

    async def info(stream: str, subjects_filter: str) -> SimpleNamespace:
        assert stream == "KV_b"
        start = subjects_filter.removesuffix(">")
        return SimpleNamespace(state=SimpleNamespace(subjects={name: 1 for name in names if name.startswith(start)}))

    async def last(stream: str, name: str) -> SimpleNamespace:
        assert stream == "KV_b"
        return values[name]

    js = SimpleNamespace(stream_info=info, get_last_msg=AsyncMock(side_effect=last), purge_stream=AsyncMock())
    asyncio.run(nats_payload.reclaim_all(js, "KV_b", prefix))
    assert js.get_last_msg.await_args_list == [call("KV_b", f"{prefix}deadlines"), call("KV_b", f"{prefix}neighbour")]
    assert js.purge_stream.await_args_list == [
        call("KV_b", subject=f"{prefix}_chunks.{stale}.0"),
        call("KV_b", subject=f"{prefix}_chunks.{stale}.1"),
        call("KV_b", subject=f"{prefix}_chunks.{orphan}.0"),
    ]
