"""A publish no stream takes is refused as NoStream, and the ledger relay then keeps only that swarm's rows."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from nats.js.errors import NoStreamResponseError

from scripts.events import nats
from scripts.events.bus import NoStream
from scripts.events.config import BusConfig
from scripts.events.envelope import create
from scripts.swarm_ledger.repository import outbox, sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.events import nats_server

CONTENT = {
    "title": "Store",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "One", "description": "d"}],
    "tasks": [{"title": "first", "phase": "p1", "lane": "eng"}],
}


class Unanswered:
    _nc = SimpleNamespace(max_payload=1_048_576)

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def publish(self, subject: str, data: bytes, stream: str, headers: dict[str, str]) -> None:
        self.sent.append((subject, stream))
        raise NoStreamResponseError


@pytest.mark.feature("ledger-event-relay")
def test_a_publish_no_stream_answers_is_refused_as_no_stream() -> None:
    js = Unanswered()
    event = create(type="task.changed", swarm="alpha", entity="task", key="t1", version=1, author="ledger", data={})
    with pytest.raises(NoStream, match=r"^swarm\.alpha\.events\.task\.t1$") as refused:
        asyncio.run(nats._publish(js, "swarm.alpha.events.task.t1", "AGENTIHOOKS-a-EVENTS", event))
    assert isinstance(refused.value.__cause__, NoStreamResponseError)
    assert js.sent == [("swarm.alpha.events.task.t1", "AGENTIHOOKS-a-EVENTS")]


@pytest.mark.feature("ledger-event-relay")
def test_a_bound_scoped_relay_publishes_the_ledgers_with_a_stream_past_one_without(tmp_path: Path) -> None:
    repository = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repository.create("alpha", CONTENT, "swarm")
    repository.create("ledger", CONTENT, "swarm")
    with nats_server.running(tmp_path / "js") as url:
        config = BusConfig(backend="nats", url=url, scoped=True)
        hub = nats.open_bus(config)
        bound = nats.open_bus(replace(config, provision=False))
        try:
            hub.for_swarm("ledger")
            assert outbox.relay(repository, bound.events) == 2
            with repository.connect() as connection:
                left = connection.execute(
                    "SELECT slug FROM outbox WHERE id NOT IN (SELECT id FROM outbox_sent) ORDER BY slug"
                ).fetchall()
            assert left == [("alpha",), ("alpha",)]
        finally:
            bound.close()
            hub.close()
