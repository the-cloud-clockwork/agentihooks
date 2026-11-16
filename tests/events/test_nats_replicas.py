"""Replica counts of the bus's JetStream streams and buckets on a three server cluster."""

import asyncio
from collections.abc import Iterator

import nats as nats_py
import pytest

from scripts.events import nats
from scripts.events.config import SCOPED_STATE_BUCKETS, BusConfig, QueuePolicy
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
