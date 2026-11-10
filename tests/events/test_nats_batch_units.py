import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from nats.js.errors import APIError

from scripts.events import nats_batch
from scripts.events.bus import PayloadTooLarge, StateEntry
from scripts.events.nats import NatsStateStore
from scripts.events.nats_state import ScopedStateStore

pytestmark = pytest.mark.unit


def _msg(subject: str, data: bytes | None, headers: dict[str, str] | None, seq: int) -> SimpleNamespace:
    return SimpleNamespace(
        subject=subject, data=data, headers=headers, metadata=SimpleNamespace(sequence=SimpleNamespace(stream=seq))
    )


class _Sub:
    def __init__(self, messages: list[SimpleNamespace]):
        self.subject = "_INBOX.batch"
        self.messages = messages
        self.timeouts: list[float | None] = []
        self.unsubscribed = False

    async def next_msg(self, timeout: float | None = 1.0) -> SimpleNamespace:
        self.timeouts.append(timeout)
        return self.messages.pop(0)

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _Client:
    def __init__(self, reply: dict[str, Any], messages: list[SimpleNamespace], max_payload: int = 1_000_000):
        self.reply = reply
        self.sub = _Sub(messages)
        self.max_payload = max_payload
        self.requests: list[tuple[str, dict[str, Any], float]] = []
        self.subscribed: list[str] = []

    def new_inbox(self) -> str:
        return "_INBOX.batch"

    async def subscribe(self, subject: str) -> _Sub:
        self.subscribed.append(subject)
        return self.sub

    async def request(self, subject: str, payload: bytes, timeout: float) -> SimpleNamespace:
        self.requests.append((subject, json.loads(payload), timeout))
        return SimpleNamespace(data=json.dumps(self.reply).encode())


def _js(client: _Client) -> Any:
    return SimpleNamespace(_nc=client, _prefix="$JS.API")


@pytest.mark.feature("bus-state-batch")
def test_last_per_subject_creates_one_last_per_subject_consumer_and_reads_what_it_counts() -> None:
    messages = [_msg("s.a", None, None, 4), _msg("s.b", b"two", {"h": "v"}, 9)]
    client = _Client({"num_pending": 1, "delivered": {"consumer_seq": 1}}, messages)
    found = asyncio.run(nats_batch.last_per_subject(_js(client), "S", ["s.a", "s.b", "s.a"]))
    assert client.subscribed == ["_INBOX.batch"]
    assert client.requests == [
        (
            "$JS.API.CONSUMER.CREATE.S",
            {
                "stream_name": "S",
                "config": {
                    "deliver_policy": "last_per_subject",
                    "ack_policy": "none",
                    "filter_subjects": ["s.a", "s.b"],
                    "deliver_subject": "_INBOX.batch",
                    "inactive_threshold": 5_000_000_000,
                    "mem_storage": True,
                    "num_replicas": 1,
                },
            },
            5.0,
        )
    ]
    assert found == {"s.a": nats_batch.Last(b"", {}, 4), "s.b": nats_batch.Last(b"two", {"h": "v"}, 9)}
    assert client.sub.timeouts == [5.0, 5.0]
    assert client.sub.unsubscribed


@pytest.mark.feature("bus-state-batch")
def test_last_per_subject_sends_nothing_for_no_subjects() -> None:
    client = _Client({}, [])
    assert asyncio.run(nats_batch.last_per_subject(_js(client), "S", [])) == {}
    assert (client.subscribed, client.requests) == ([], [])


@pytest.mark.feature("bus-state-batch")
def test_last_per_subject_raises_the_create_error_and_unsubscribes() -> None:
    client = _Client({"error": {"code": 400, "err_code": 10003, "description": "bad"}}, [])
    with pytest.raises(APIError):
        asyncio.run(nats_batch.last_per_subject(_js(client), "S", ["s.a"]))
    assert client.sub.unsubscribed


@pytest.mark.feature("bus-state-batch")
def test_last_per_subject_refuses_a_create_over_the_payload_limit() -> None:
    client = _Client({}, [], max_payload=64)
    with pytest.raises(PayloadTooLarge):
        asyncio.run(nats_batch.last_per_subject(_js(client), "S", ["s." + "x" * 80]))
    assert client.requests == []
    assert client.sub.unsubscribed


@pytest.mark.feature("bus-state-batch")
def test_read_entries_keeps_key_order_and_reads_deleted_or_missing_keys_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[list[str]] = []

    async def last(js: Any, stream: str, subjects: list[str]) -> dict[str, nats_batch.Last]:
        asked.append([stream, *subjects])
        return {
            "p.a": nats_batch.Last(b"one", {}, 3),
            "p.gone": nats_batch.Last(b"", {"KV-Operation": "DEL"}, 5),
        }

    async def chunk(key: str) -> bytes:
        return b""

    monkeypatch.setattr(nats_batch, "last_per_subject", last)
    keys = ["a", "missing", "gone", "a"]
    entries = asyncio.run(
        nats_batch.read_entries(_js(_Client({}, [])), "S", keys, lambda key: f"p.{key}", chunk, lambda m: m.seq * 10)
    )
    entry = StateEntry(key="a", value=b"one", revision=30)
    assert entries == [entry, None, None, entry]
    assert asked == [["S", "p.a", "p.missing", "p.gone", "p.a"]]


def _wiring(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def read(*args: Any) -> str:
        calls.append(args)
        return "read"

    monkeypatch.setattr(nats_batch, "read_entries", read)
    return calls


def _loop() -> Any:
    return SimpleNamespace(run=lambda work: [work])


@pytest.mark.feature("bus-state-batch")
def test_the_key_value_store_reads_many_keys_through_its_bucket_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wiring(monkeypatch)
    store = object.__new__(NatsStateStore)
    js: Any = "js"
    store._loop, store._js, store._bucket = _loop(), js, "names"
    assert store.get_many(["k"]) == ["read"]
    assert calls == [("js", "KV_names", ["k"], store._kv_subject, store._read_chunk, store._last_revision)]
    assert store._kv_subject("alias.x") == "$KV.names.alias.x"
    assert NatsStateStore._last_revision(nats_batch.Last(b"", {}, 7)) == 7


@pytest.mark.feature("bus-state-batch")
def test_the_scoped_store_reads_many_keys_through_its_swarm_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wiring(monkeypatch)
    store = object.__new__(ScopedStateStore)
    root: Any = SimpleNamespace(_js="js", _loop=_loop())
    store._root, store._stream = root, "STATE"
    assert store.get_many(["k"]) == ["read"]
    assert calls == [("js", "STATE", ["k"], store._subject, store._read_chunk, store._revision)]
