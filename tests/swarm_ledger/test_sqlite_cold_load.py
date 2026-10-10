import threading
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.swarm_ledger.repository import sqlite
from tests.swarm_ledger.test_sqlite import CONTENT, chat, store


@pytest.mark.parametrize("state", ["cold", "external_write", "bound_twin"])
def test_concurrent_readers_share_one_generation_load(tmp_path, monkeypatch, state):
    writer = store(tmp_path)
    writer.create("ledger", CONTENT)
    monkeypatch.setattr(writer.domain, "LEDGER_DIR", writer.directory)
    reader = sqlite.SQLiteLedgerRepository()
    with reader.connect():
        pass
    if state == "external_write":
        reader.get_document("ledger")
        writer.apply_ops("ledger", ops=[chat(1)])
    twin = reader.bound(types.SimpleNamespace(**vars(reader.domain))) if state == "bound_twin" else reader
    entered, duplicate, release = threading.Event(), threading.Event(), threading.Event()
    start = threading.Barrier(8)
    loads = []
    original = sqlite.read_rows

    def blocked_load(connection, slug):
        loads.append(slug)
        entered.set()
        if len(loads) > 1:
            duplicate.set()
        assert release.wait(5)
        return original(connection, slug)

    def read(repo):
        start.wait(timeout=5)
        return repo.get_document("ledger")

    monkeypatch.setattr(sqlite, "read_rows", blocked_load)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read, repo) for repo in [reader, twin] * 4]
        try:
            assert entered.wait(5)
            assert not duplicate.wait(0.3), "concurrent requests duplicate the whole ledger load"
        finally:
            release.set()
        documents = [future.result(timeout=5) for future in futures]
    assert loads == ["ledger"]
    assert all(document == documents[0] for document in documents)
    assert documents[0]["title"] == CONTENT["title"]
    if state == "external_write":
        assert documents[0]["chat"][0]["id"] == "m1"
