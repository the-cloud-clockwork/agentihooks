from pathlib import Path

import pytest

from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

CONTENT = {"title": "Size", "overview": "o", "sources": [], "phases": [{"title": "One", "description": "d"}]}
SMALL = {"op": "size_set", "id": "s1", "by": "swarm", "size": "small"}
REFUSAL = "size_set refused: swarm size-bound works this ledger and a small ledger sends it no events"


@pytest.fixture
def swarms(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    monkeypatch.setattr("scripts.swarm.store.connect", lambda environ=None: store)
    return store


def ledger(tmp_path: Path, slug: str) -> SQLiteLedgerRepository:
    repo = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repo.create(slug, CONTENT, "swarm")
    return repo


@pytest.mark.feature("ledger-size-small-refused-with-swarm")
def test_size_small_is_refused_on_a_ledger_its_swarm_works_and_the_size_stays_swarm(
    tmp_path: Path, swarms: RedisStore
) -> None:
    repo = ledger(tmp_path, "size-bound")
    swarms.create(SwarmConfig("size-bound", "/repo", 1, 0))

    state, rejected = repo.apply_ops("size-bound", ops=[SMALL])

    assert rejected == ["s1"]
    assert REFUSAL in state["_meta"]["warnings"]
    assert state["size"] == "swarm"
    assert repo.get_document("size-bound")["size"] == "swarm"


@pytest.mark.feature("ledger-size-small-refused-with-swarm")
def test_size_small_is_set_on_a_ledger_no_swarm_works(tmp_path: Path, swarms: RedisStore) -> None:
    repo = ledger(tmp_path, "size-free")
    swarms.create(SwarmConfig("size-other", "/repo", 1, 0))

    state, rejected = repo.apply_ops("size-free", ops=[SMALL])

    assert rejected == []
    assert state["_meta"]["warnings"] == []
    assert state["size"] == "small"
    assert repo.get_document("size-free")["size"] == "small"
