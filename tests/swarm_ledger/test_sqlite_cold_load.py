import threading
import time
import types

from scripts.swarm_ledger.repository import sqlite
from tests.swarm_ledger.test_sqlite import CONTENT, store

READERS = 8


def counted_loads(monkeypatch):
    loads = []
    real = sqlite.read_rows

    def slow_read_rows(connection, slug):
        loads.append(slug)
        time.sleep(0.2)
        return real(connection, slug)

    monkeypatch.setattr(sqlite, "read_rows", slow_read_rows)
    return loads


def read_together(*repositories):
    start = threading.Barrier(len(repositories))
    documents = [None] * len(repositories)

    def read(index, repository):
        start.wait()
        documents[index] = repository.get_document("ledger")

    threads = [threading.Thread(target=read, args=pair) for pair in enumerate(repositories)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return documents


def test_concurrent_readers_of_a_cold_ledger_share_one_load(tmp_path, monkeypatch):
    r = store(tmp_path)
    r.create("ledger", CONTENT)
    r._cache.clear()
    loads = counted_loads(monkeypatch)
    documents = read_together(*[r] * READERS)
    assert loads == ["ledger"]
    assert all(document == documents[0] for document in documents)
    assert documents[0]["title"] == CONTENT["title"]


def test_a_bound_twin_shares_the_cold_load(tmp_path, monkeypatch):
    r = sqlite.SQLiteLedgerRepository()
    monkeypatch.setattr(r.domain, "LEDGER_DIR", tmp_path / "ledgers")
    r.create("ledger", CONTENT)
    r._cache.clear()
    twin = r.bound(types.SimpleNamespace(**vars(r.domain)))
    assert twin is not r
    loads = counted_loads(monkeypatch)
    read_together(r, twin, r, twin)
    assert loads == ["ledger"]
