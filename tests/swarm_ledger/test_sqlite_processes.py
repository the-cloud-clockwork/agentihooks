import multiprocessing
import os

from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.test_sqlite import CONTENT


def join(path, index):
    repo = SQLiteLedgerRepository(path)
    for turn in range(3):
        repo.apply_ops("processes", ops=[{"op": "join", "id": f"j{index}-{turn}", "by": f"eng{index}"}])
        repo.apply_ops("processes", ops=[{"op": "add", "id": f"m{index}-{turn}", "thread": "chat", "text": "hi"}])


def crash(path):
    class Exit:
        def apply(self, doc, op, ctx, apply_op):
            apply_op(doc, op, ctx)
            os._exit(7)

    SQLiteLedgerRepository(path).apply_ops(
        "processes", ops=[{"op": "add", "id": "lost", "thread": "chat", "text": "never stored"}], gate=Exit()
    )


def test_concurrent_agent_processes_each_land_every_write(tmp_path):
    path = tmp_path / "ledgers" / sqlite.DATABASE
    SQLiteLedgerRepository(path).create("processes", CONTENT)
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=join, args=(path, index)) for index in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0
    state = SQLiteLedgerRepository(path).get_document("processes")
    assert {f"eng{index}" for index in range(4)} <= set(state["_meta"]["members"])
    assert sorted(m["id"] for m in state["chat"]) == sorted(f"m{i}-{t}" for i in range(4) for t in range(3))


def test_a_process_that_dies_mid_write_leaves_the_last_committed_state(tmp_path):
    path = tmp_path / "ledgers" / sqlite.DATABASE
    repo = SQLiteLedgerRepository(path)
    repo.create("processes", CONTENT)
    before = repo.get_document("processes")
    worker = multiprocessing.get_context("fork").Process(target=crash, args=(path,))
    worker.start()
    worker.join(30)
    assert worker.exitcode == 7
    assert SQLiteLedgerRepository(path).get_document("processes") == before
    state, _ = repo.apply_ops("processes", ops=[{"op": "add", "id": "after", "thread": "chat", "text": "ok"}])
    assert [m["id"] for m in state["chat"]] == ["after"]
