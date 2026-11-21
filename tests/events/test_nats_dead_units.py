import asyncio
import base64
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.events import nats
from scripts.events.config import QueuePolicy

pytestmark = pytest.mark.unit


def _event(payload: bytes = b"raw") -> nats.Envelope:
    message = SimpleNamespace(
        data=payload,
        subject="ci.lab.head.push",
        metadata=SimpleNamespace(stream=nats.LAB_STREAM, sequence=SimpleNamespace(stream=7)),
    )
    return nats._undecodable(cast(nats.Msg, message))


def _message(event: nats.Envelope, stream: str, subject: str = "dead.unknown.root") -> nats.Msg:
    return cast(
        nats.Msg,
        SimpleNamespace(
            data=event.encode(),
            subject=subject,
            headers=None,
            metadata=SimpleNamespace(stream=stream, num_delivered=1),
            term=AsyncMock(),
        ),
    )


@pytest.mark.feature("lab-head-undecodable-dead-letter")
def test_small_multipart_payloads_fill_the_reserved_budget_and_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = bytes(range(256)) * 8
    event = _event(raw)
    letter = replace(nats.dead_letter(event, "unit", 1), time=0)
    monkeypatch.setattr(nats, "dead_letter", Mock(return_value=letter))
    budget = 2048 - nats.nats_payload.KV_HEADER_BUDGET
    stored = dict[str, bytes]()
    sizes = list[int]()

    async def publish(subject: str, data: bytes, *, stream: str, headers: dict[str, str]) -> SimpleNamespace:
        assert stream == nats.DEAD_STREAM
        assert len(stored) < 8, "multipart publication must remain bounded"
        assert len(data) <= budget
        sizes.append(len(data))
        stored[subject] = data
        return SimpleNamespace(duplicate=False)

    async def read(stream: str, subject: str) -> SimpleNamespace:
        assert stream == nats.DEAD_STREAM
        return SimpleNamespace(data=stored[subject])

    js = cast(
        nats.JetStreamContext,
        SimpleNamespace(
            _nc=SimpleNamespace(max_payload=2048),
            stream_info=AsyncMock(side_effect=nats.NotFoundError()),
            publish=publish,
            get_last_msg=read,
        ),
    )
    asyncio.run(nats._publish_dead(js, event, "unit", 1))
    assert len(stored) > 2
    assert all(size == budget for size in sizes[:-2])
    target = list(stored)[-1]
    manifest = nats.Envelope.decode(stored[target])
    assert manifest.data["event"]["data"]["payload_parts"]
    restored = asyncio.run(nats._hydrate_dead(js, _message(manifest, nats.DEAD_STREAM, target), manifest))
    assert restored.data["event"] == event.to_dict()
    assert base64.b64decode(restored.data["event"]["data"]["payload"], validate=True) == raw


@pytest.mark.feature("lab-head-undecodable-dead-letter")
def test_a_letter_fitting_the_reserved_budget_is_published_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    event = _event()
    letter = replace(nats.dead_letter(event, "unit", 1), time=0)
    monkeypatch.setattr(nats, "dead_letter", Mock(return_value=letter))
    publish = AsyncMock(return_value=SimpleNamespace(duplicate=False))
    js = cast(
        nats.JetStreamContext,
        SimpleNamespace(
            _nc=SimpleNamespace(max_payload=len(letter.encode()) + nats.nats_payload.KV_HEADER_BUDGET),
            stream_info=AsyncMock(side_effect=nats.NotFoundError()),
            publish=publish,
        ),
    )
    asyncio.run(nats._publish_dead(js, event, "unit", 1))
    assert publish.await_count == 1
    assert publish.await_args is not None
    assert publish.await_args.args[1] == letter.encode()


@pytest.mark.feature("lab-head-undecodable-dead-letter")
@pytest.mark.parametrize("changed", ["id", "payload"])
def test_conflicting_retained_parts_never_complete_publication(changed: str) -> None:
    letter = replace(nats.dead_letter(_event(), "unit", 1), type="dead_payload_chunk", data={"payload": "kept"})
    retained = replace(letter, id="other") if changed == "id" else replace(letter, data={"payload": "different"})
    error = nats.APIError(err_code=10071)
    js = cast(
        nats.JetStreamContext,
        SimpleNamespace(
            _nc=SimpleNamespace(max_payload=2048),
            publish=AsyncMock(side_effect=error),
            get_last_msg=AsyncMock(return_value=SimpleNamespace(data=retained.encode())),
        ),
    )
    with pytest.raises(nats.APIError) as raised:
        asyncio.run(nats._publish_letter(js, letter, nats.DEAD_STREAM, "dead.unknown.part"))
    assert raised.value is error


@pytest.mark.feature("lab-head-undecodable-dead-letter")
@pytest.mark.parametrize("changed", ["id", "type"])
def test_hydration_rejects_a_part_with_the_wrong_identity_or_type(changed: str) -> None:
    event = _event()
    original = replace(event, data={"subject": "ci.lab.head.push", "payload_parts": ["part"]})
    letter = nats.dead_letter(original, "unit", 1)
    part = replace(event, id="part", type="dead_payload_chunk", data={"payload": "eA=="})
    part = replace(part, id="other") if changed == "id" else replace(part, type="ordinary")
    js = cast(
        nats.JetStreamContext,
        SimpleNamespace(get_last_msg=AsyncMock(return_value=SimpleNamespace(data=part.encode()))),
    )
    with pytest.raises(ValueError):
        asyncio.run(nats._hydrate_dead(js, _message(letter, nats.DEAD_STREAM), letter))


@pytest.mark.feature("lab-head-undecodable-dead-letter")
@pytest.mark.parametrize(
    "stream",
    [
        nats.DEAD_STREAM,
        "AGENTIHOOKS_DEAD",
        nats.stream_name("proof", "DEAD"),
        nats.legacy_stream_name("proof", "DEAD"),
    ],
)
def test_dead_readers_refill_all_stream_names_with_only_the_remaining_budget(
    monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    letter = nats.dead_letter(_event(), "unit", 1)
    part = replace(letter, type="dead_payload_chunk")
    part_message = _message(part, stream)
    fetch = AsyncMock(side_effect=[[part_message], [_message(letter, stream)]])
    clock = SimpleNamespace(time=Mock(side_effect=[0.0, 0.0, 0.04, 0.04]))
    monkeypatch.setattr(nats, "asyncio", SimpleNamespace(get_running_loop=lambda: clock, TimeoutError=TimeoutError))
    consumer = nats.NatsConsumer(
        cast(nats._Loop, SimpleNamespace()),
        cast(nats.JetStreamContext.PullSubscription, SimpleNamespace(fetch=fetch)),
        cast(nats.JetStreamContext, SimpleNamespace()),
        QueuePolicy(),
        "unit",
        swarm="proof",
    )
    assert [delivery.event for delivery in asyncio.run(consumer._fetch(1, 100))] == [letter]
    assert fetch.await_count == 2
    assert fetch.await_args_list[0].kwargs["timeout"] == 0.1
    assert fetch.await_args_list[1].kwargs["timeout"] == pytest.approx(0.06)
    cast(AsyncMock, part_message.term).assert_awaited_once_with()


@pytest.mark.feature("lab-head-undecodable-dead-letter")
def test_dead_readers_stop_at_the_exact_refill_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    letter = nats.dead_letter(_event(), "unit", 1)
    part = replace(letter, type="dead_payload_chunk")
    fetch = AsyncMock(side_effect=[[_message(part, nats.DEAD_STREAM)], [_message(letter, nats.DEAD_STREAM)]])
    clock = SimpleNamespace(time=Mock(side_effect=[0.0, 0.0, 0.1, 0.1]))
    monkeypatch.setattr(nats, "asyncio", SimpleNamespace(get_running_loop=lambda: clock, TimeoutError=TimeoutError))
    consumer = nats.NatsConsumer(
        cast(nats._Loop, SimpleNamespace()),
        cast(nats.JetStreamContext.PullSubscription, SimpleNamespace(fetch=fetch)),
        cast(nats.JetStreamContext, SimpleNamespace()),
        QueuePolicy(),
        "unit",
    )
    assert asyncio.run(consumer._fetch(1, 100)) == []
    fetch.assert_awaited_once_with(1, timeout=0.1)
