import base64
import io
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch

import pytest

if TYPE_CHECKING:
    from scripts.events.redis_stream import BulkReader

pytestmark = pytest.mark.feature("scheduler-bounded-history")


def timer(name: str, state: str = "fired", owner: str = "process") -> dict[str, Any]:
    return {"id": name, "kind": "timer", "owner": owner, "state": state, "due": 1, "payload": name}


def envelope(document: dict[str, Any], revision: int = 1) -> bytes:
    value = base64.b64encode(json.dumps(document).encode()).decode()
    return json.dumps({"revision": revision, "value": value}).encode()


@pytest.mark.feature("scheduler-bounded-history")
@pytest.mark.parametrize("retired_first", [False, True])
def test_streaming_recovery_keeps_active_pending_and_operator_timers(retired_first: bool) -> None:
    from scripts.events.redis_stream import BulkReader, Envelope
    from scripts.events.scheduler_records import compact

    document = {
        "entries": {
            **{str(i): timer(str(i)) for i in range(1_000)},
            "active": timer("active", "active"),
            "pending": timer("pending", "pending"),
            "operator": timer("operator", owner="operator"),
            "retired": timer("retired", "cancelled", "operator"),
        },
        "lease": {"token": "holder", "expires": 20_000},
        "retired": {"retired": True},
    }
    if retired_first:
        document = {"retired": document["retired"], **document}
    raw = envelope(document)
    decoded = Envelope(BulkReader(io.BytesIO(raw), len(raw)))
    assert compact(decoded, 10_000) == {
        "entries": {name: document["entries"][name] for name in ("active", "pending", "operator")},
        "lease": document["lease"],
        "retired": {},
    }
    assert decoded.revision == 1


@pytest.mark.feature("scheduler-bounded-history")
def test_envelope_reads_base64_in_bounded_chunks() -> None:
    from scripts.events import redis_stream
    from scripts.events.redis_stream import BulkReader, Envelope

    raw = envelope({"payload": "a" * (redis_stream.CHUNK * 4)})
    source = BulkReader(io.BytesIO(raw), len(raw))
    decoded = Envelope(source)
    chunks = []
    while chunk := decoded.read(1_000):
        chunks.append(chunk)
        assert len(decoded.encoded) < 4
        assert len(decoded.decoded) <= redis_stream.CHUNK
    assert json.loads(b"".join(chunks))["payload"] == "a" * (redis_stream.CHUNK * 4)


@pytest.mark.feature("scheduler-bounded-history")
def test_raw_redis_field_disconnects_its_dedicated_connection() -> None:
    from scripts.events import redis_stream

    connection = Mock()
    connection._sock.makefile.return_value = io.BytesIO(b"$5\r\nhello\r\n")
    redis = Mock()

    def connection_for(command_name: str) -> Mock:
        assert command_name == "HGET"
        return connection

    redis.connection_pool.get_connection.side_effect = connection_for
    with redis_stream.field(redis, "state", "entries") as source:
        assert source.read(3) == b"hel"
        assert source.read(100) == b"lo"
        assert source.read(1) == b""
    connection.send_command.assert_called_once_with("HGET", "state", "entries")
    connection.disconnect.assert_called_once_with()
    redis.connection_pool.release.assert_called_once_with(connection)


@pytest.mark.parametrize("header", [b"$-1\r\n", b"-NOPERM denied\r\n", b"+OK\r\n", b"$4\r\nx"])
@pytest.mark.feature("scheduler-bounded-history")
def test_raw_redis_field_handles_missing_errors_and_truncated_values(header: bytes) -> None:
    from redis.exceptions import ResponseError

    from scripts.events import redis_stream

    connection, redis = Mock(), Mock()
    connection._sock.makefile.return_value = io.BytesIO(header)
    redis.connection_pool.get_connection.return_value = connection
    if header.startswith(b"$-1"):
        with redis_stream.field(redis, "state", "entries") as source:
            assert source is None
    else:
        with pytest.raises((ResponseError, ValueError, EOFError)):
            with redis_stream.field(redis, "state", "entries") as source:
                source.read(4)
                source.read(4)
    connection.disconnect.assert_called_once_with()


@pytest.mark.feature("scheduler-bounded-history")
def test_existing_oversized_state_is_compacted_without_a_full_hget(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis

    from scripts.events import redis_stream, scheduler_records
    from scripts.events.redis_stream import BulkReader
    from scripts.events.scheduler_records import SchedulerState

    redis = fakeredis.FakeRedis(decode_responses=True)
    state = SchedulerState(redis, "scheduler")
    key = "headed.entries"
    document = {"entries": {str(i): timer(str(i)) for i in range(100)}, "retired": {}}
    document["entries"]["keep"] = timer("keep", "pending")
    raw = envelope(document)
    redis.hset(state._current, key, raw)
    redis.set(state._revision, 1)
    redis.rpush(state._history + key, raw, raw)
    redis.hset("headed:entries", mapping={str(i): json.dumps(timer(str(i))) for i in range(100)})

    @contextmanager
    def field(client: Any, name: str, item: str) -> Iterator["BulkReader"]:
        assert client is redis and name == state._current and item == key
        yield BulkReader(io.BytesIO(raw), len(raw))

    monkeypatch.setattr(scheduler_records, "MAX_DOCUMENT", 1_024)
    monkeypatch.setattr(redis_stream, "field", field)
    original = redis.hget

    def bounded(name: str, item: str) -> Any:
        value = original(name, item)
        assert name != state._current or value is None or len(value) < 1_024
        return value

    with patch.object(redis, "hget", side_effect=bounded):
        stored = state.get(key)
    assert set(json.loads(stored.value)["entries"]) == {"keep"}
    assert state.scheduler_record(key, "keep") == document["entries"]["keep"]
    assert redis.llen(state._history + key) == 1
    assert redis.hlen("headed:entries") == 0


@pytest.mark.feature("scheduler-bounded-history")
def test_recovery_retries_a_concurrent_write_and_keeps_its_live_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis

    from scripts.events import redis_stream
    from scripts.events.redis_stream import BulkReader
    from scripts.events.scheduler_records import SchedulerState

    redis = fakeredis.FakeRedis(decode_responses=True)
    state = SchedulerState(redis, "scheduler")
    key = "headed.entries"
    before = envelope({"entries": {str(i): timer(str(i)) for i in range(20)}, "retired": {}})
    after = envelope({"entries": {"new": timer("new", "active")}, "retired": {}}, 2)
    redis.hset(state._current, key, before)
    redis.set(state._revision, 1)
    calls = []

    @contextmanager
    def racing(client: Any, name: str, item: str) -> Iterator["BulkReader"]:
        calls.append(item)
        raw = before if len(calls) == 1 else after
        if len(calls) == 1:
            client.hset(name, item, after)
            client.set(state._revision, 2)
        yield BulkReader(io.BytesIO(raw), len(raw))

    monkeypatch.setattr(redis_stream, "field", racing)
    state._compact(key)
    assert len(calls) == 2
    assert state.scheduler_record(key, "new")["state"] == "active"
