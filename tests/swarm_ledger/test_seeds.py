import copy

from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.test_sqlite import document


def test_retained_seeds_export_through_their_deltas(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    state = document()
    for revision in range(2, 12):
        state["title"] = f"Title {revision}"
        state["_meta"]["rev"] = revision
        state["_meta"]["seeds"][str(revision)] = copy.deepcopy({k: v for k, v in state.items() if k != "_meta"})
        state["_meta"]["seeds"] = {
            key: value for key, value in state["_meta"]["seeds"].items() if int(key) > revision - 5
        }
        repo.import_document("seeds", state, replace=True)
        assert repo.export_document("seeds") == state
        assert repo.get_document("seeds")["_meta"]["seeds"] == {}
    with repo.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM seed_deltas").fetchone()[0] == 4
    state["_meta"]["seeds"] = {}
    repo.import_document("seeds", state, replace=True)
    assert repo.export_document("seeds") == state


def test_seed_delta_removes_fields_and_threads(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    state = document()
    current = copy.deepcopy(state["_meta"]["seeds"]["1"])
    current["tasks"][1].pop("unknown")
    current["questions"] = []
    current["extension"] = None
    state["_meta"]["seeds"]["2"] = current
    state["_meta"]["rev"] = 2
    repo.import_document("seeds", state)
    assert repo.export_document("seeds") == state
    state["_meta"]["seeds"]["2"]["tasks"] = []
    repo.import_document("seeds", state, replace=True)
    assert repo.export_document("seeds") == state


def test_a_document_without_seeds_exports_without_them(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    state = document()
    state["_meta"].pop("seeds")
    repo.import_document("bare", state)
    assert repo.export_document("bare") == state
    assert "seeds" not in repo.get_document("bare")["_meta"]
