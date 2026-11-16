"""Replica counts of the bus's JetStream streams and buckets, on a three server cluster and against a faked server."""

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call

import nats as nats_py
import pytest
from nats.js.api import StreamConfig
from nats.js.errors import APIError

from scripts.events import nats, nats_replicas
from scripts.events.config import REPLICAS_ENV, SCOPED_STATE_BUCKETS, BusConfig, QueuePolicy
from scripts.events.subjects import STOPS, stream_name

from . import nats_server

pytestmark = pytest.mark.unit

FEATURE = "swarm-streams-hold-the-bus-replica-count"
REPLICAS = "AGENTIHOOKS_BUS_REPLICAS"
COUNT = 3
POLICY = QueuePolicy(max_deliver=2, ack_wait_ms=1000)
SWARM = "replicas"
ORPHAN = "ORPHAN"
QUEUES = 2


@pytest.fixture(scope="module")
def servers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[list[str]]:
    with nats_server.cluster(tmp_path_factory.mktemp("cluster")) as urls:
        yield urls


@pytest.fixture
def url(servers: list[str]) -> str:
    asyncio.run(_wipe(servers[0]))
    return servers[0]


async def _wipe(url: str) -> None:
    client = await nats_py.connect(url)
    try:
        js = client.jetstream()
        for info in await js.streams_info():
            assert info.config.name
            await js.delete_stream(info.config.name)
    finally:
        await client.close()


async def _read(url: str) -> dict[str | None, tuple[int | None, int]]:
    client = await nats_py.connect(url)
    try:
        return {
            info.config.name: (info.config.num_replicas, info.state.messages)
            for info in await client.jetstream().streams_info()
        }
    finally:
        await client.close()


async def _write(url: str) -> None:
    client = await nats_py.connect(url)
    try:
        js = client.jetstream()
        await js.add_stream(name=ORPHAN, subjects=[f"{ORPHAN.lower()}.>"])
        await js.publish(f"{ORPHAN.lower()}.kept", b"kept")
        await (await js.key_value("hub-github")).put("pull", b"open")
    finally:
        await client.close()


def _bus(url: str, replicas: int, scoped: bool) -> nats.NatsBus:
    environ = {"AGENTIHOOKS_BUS_URL": url, REPLICAS: str(replicas), "AGENTIHOOKS_BUS_SCOPED": "1" if scoped else "0"}
    return nats.open_bus(BusConfig.from_environ(environ))


def _provision(url: str, replicas: int) -> None:
    scoped = _bus(url, replicas, scoped=True)
    try:
        scoped.state("hub-github")
        scoped.state("hub-timers")
        scoped.for_swarm(SWARM).queue("eng", POLICY)
    finally:
        scoped.close()
    root = _bus(url, replicas, scoped=False)
    try:
        root.queue("work", POLICY)
    finally:
        root.close()


@pytest.mark.feature(FEATURE)
def test_a_replicated_bus_creates_every_stream_and_bucket_at_its_replica_count(url: str) -> None:
    _provision(url, COUNT)
    held = asyncio.run(_read(url))
    expected = {
        nats.EVENT_STREAM,
        nats.DEAD_STREAM,
        nats.LAB_STREAM,
        "KV_hub-github",
        "KV_hub-timers",
        stream_name(SWARM, "EVENTS"),
        stream_name(SWARM, "DEAD"),
        stream_name(SWARM, STOPS),
        *(stream_name(SWARM, f"STATE_{bucket}") for bucket in SCOPED_STATE_BUCKETS),
    }
    assert expected <= set(held)
    assert len(held) == len(expected) + QUEUES
    assert {replicas for replicas, _ in held.values()} == {COUNT}


@pytest.mark.feature(FEATURE)
def test_existing_single_replica_streams_and_buckets_are_raised_in_place_keeping_their_messages(url: str) -> None:
    _provision(url, 1)
    asyncio.run(_write(url))
    before = asyncio.run(_read(url))
    assert {replicas for replicas, _ in before.values()} == {1}
    assert before[ORPHAN][1] == before["KV_hub-github"][1] == 1
    _provision(url, COUNT)
    assert asyncio.run(_read(url)) == {name: (COUNT, messages) for name, (_, messages) in before.items()}


@pytest.mark.feature(FEATURE)
def test_a_bus_at_the_default_count_keeps_replicated_streams_at_their_count(url: str) -> None:
    _provision(url, COUNT)
    _provision(url, 1)
    assert {replicas for replicas, _ in asyncio.run(_read(url)).values()} == {COUNT}


@pytest.mark.feature(FEATURE)
def test_the_replica_count_defaults_to_one_and_comes_from_the_environment() -> None:
    assert BusConfig().replicas == BusConfig.from_environ({}).replicas == 1
    assert REPLICAS_ENV == REPLICAS
    assert BusConfig.from_environ({REPLICAS: "3"}).replicas == COUNT


@pytest.mark.parametrize("count", [0, -3, True, 2.0, "3"])
def test_a_replica_count_that_is_not_a_positive_whole_number_is_refused(count: object) -> None:
    with pytest.raises(ValueError):
        BusConfig(replicas=count)  # pyright: ignore[reportArgumentType]


def test_a_replica_count_setting_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ValueError):
        BusConfig.from_environ({REPLICAS: "three"})


def _info(name: str, held: int | None) -> SimpleNamespace:
    return SimpleNamespace(config=StreamConfig(name=name, subjects=[name.lower()], num_replicas=held))


def _faked(held: int | None, *created: object) -> Any:
    return SimpleNamespace(
        add_stream=AsyncMock(side_effect=created),
        stream_info=AsyncMock(return_value=_info("S", held)),
        update_stream=AsyncMock(side_effect=lambda config: SimpleNamespace(config=config)),
    )


def test_a_create_meeting_a_stream_with_fewer_replicas_raises_it_and_creates_at_the_count() -> None:
    js = _faked(None, APIError(err_code=nats_replicas.NAME_IN_USE), "created")
    assert asyncio.run(nats_replicas.create(js, 2, "S", subjects=["s"])) == "created"
    [update] = js.update_stream.await_args_list
    assert update.kwargs["config"].num_replicas == 2
    assert update.kwargs["config"].subjects == ["s"]
    assert js.add_stream.await_args_list == [call(name="S", num_replicas=2, subjects=["s"])] * 2


def test_a_create_meeting_a_stream_with_more_replicas_keeps_its_count() -> None:
    js = _faked(COUNT, APIError(err_code=nats_replicas.NAME_IN_USE), "kept")
    assert asyncio.run(nats_replicas.create(js, 1, "S", subjects=["s"])) == "kept"
    js.update_stream.assert_not_awaited()
    assert js.add_stream.await_args_list == [
        call(name="S", num_replicas=1, subjects=["s"]),
        call(name="S", num_replicas=COUNT, subjects=["s"]),
    ]


def test_a_create_meeting_a_stream_at_the_same_count_raises_its_other_difference() -> None:
    js = _faked(2, APIError(err_code=nats_replicas.NAME_IN_USE))
    with pytest.raises(APIError) as caught:
        asyncio.run(nats_replicas.create(js, 2, "S", subjects=["s"]))
    assert caught.value.err_code == nats_replicas.NAME_IN_USE
    js.update_stream.assert_not_awaited()
    js.add_stream.assert_awaited_once()


def test_a_create_failing_for_another_reason_raises_without_reading_the_stream() -> None:
    js = _faked(1, APIError(err_code=10059))
    with pytest.raises(APIError) as caught:
        asyncio.run(nats_replicas.create(js, COUNT, "S", subjects=["s"]))
    assert caught.value.err_code == 10059
    js.stream_info.assert_not_awaited()


def test_raise_all_raises_each_stream_below_the_count_across_every_page() -> None:
    low, high = _info("LOW", 1), _info("HIGH", COUNT)
    js: Any = SimpleNamespace(
        streams_info=AsyncMock(side_effect=[[low], [high], []]),
        update_stream=AsyncMock(side_effect=lambda config: SimpleNamespace(config=config)),
    )
    asyncio.run(nats_replicas.raise_all(js, COUNT))
    assert js.streams_info.await_args_list == [call(offset=0), call(offset=1), call(offset=2)]
    [update] = js.update_stream.await_args_list
    assert (update.kwargs["config"].name, update.kwargs["config"].num_replicas) == ("LOW", COUNT)
