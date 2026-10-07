import copy
import multiprocessing
import os

from scripts.swarm_ledger.repository import FileLedgerRepository
from scripts.swarm_ledger.repository.file import core
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.test_sqlite import document


def join(directory, index):
    core.LEDGER_DIR = directory
    FileLedgerRepository().apply_ops("processes", ops=[{"op": "join", "id": f"j{index}", "by": f"eng{index}"}])


def crash(database, state):
    repo = SQLiteLedgerRepository(database)
    repo.verify = lambda *args: os._exit(7)
    repo.import_document("crash", state)


def test_process_crash_rolls_back_sqlite(tmp_path):
    context = multiprocessing.get_context("fork")
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    before = document()
    repo.import_document("crash", before)
    after = copy.deepcopy(before)
    after["_meta"]["rev"] = 2
    after["title"] = "Crash"
    after["_meta"]["events"].append({"rev": 2, "id": "crash"})
    worker = context.Process(target=crash, args=(repo.path, after))
    worker.start()
    worker.join(10)
    assert worker.exitcode == 7
    assert SQLiteLedgerRepository(repo.path).get_document("crash") == before


def test_separate_processes_serialize_file_and_shadow_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setenv("LEDGER_SQLITE_SHADOW", "1")
    repo = FileLedgerRepository()
    repo.create(
        "processes",
        {"title": "Processes", "overview": "o", "sources": [], "phases": [{"title": "One", "description": "d"}]},
    )
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=join, args=(tmp_path, index)) for index in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    state = repo.get_document("processes", reconcile=False)
    assert all(f"eng{index}" in state["_meta"]["members"] for index in range(4))
    assert SQLiteLedgerRepository(tmp_path / "ledger-shadow.sqlite3").get_document("processes") == state
