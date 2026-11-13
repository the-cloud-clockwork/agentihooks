from pathlib import Path

import pytest

from scripts.events import nats, nats_batch
from scripts.events.config import BusConfig
from tests.events import nats_server

pytestmark = pytest.mark.integration


@pytest.mark.feature("bus-state-batch")
def test_last_per_subject_answers_the_newest_message_of_each_held_subject(tmp_path: Path) -> None:
    with nats_server.running(tmp_path / "jetstream") as url:
        bus = nats.open_bus(BusConfig(backend="nats", url=url))
        try:
            store = bus.state("batch")
            first = store.create("a", b"one")
            second = store.update("a", b"two", first)
            store.create("b", b"three")
            subjects = ["$KV.batch.a", "$KV.batch.missing", "$KV.batch.b", "$KV.batch.a"]
            found = bus._loop.run(nats_batch.last_per_subject(bus._js, "KV_batch", subjects))
            assert sorted(found) == ["$KV.batch.a", "$KV.batch.b"]
            assert (found["$KV.batch.a"].data, found["$KV.batch.a"].seq) == (b"two", second)
            assert found["$KV.batch.b"].data == b"three"
            assert bus._loop.run(nats_batch.last_per_subject(bus._js, "KV_batch", [])) == {}
        finally:
            bus.close()
