import copy

from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.test_sqlite import document


def test_retained_seeds_replay_after_checkpoint_advances(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "shadow.sqlite3")
    state = document()
    for revision in range(2, 12):
        state["title"] = f"Title {revision}"
        state["_meta"]["rev"] = revision
        state["_meta"]["seeds"][str(revision)] = copy.deepcopy({k: v for k, v in state.items() if k != "_meta"})
        state["_meta"]["seeds"] = {
            key: value for key, value in state["_meta"]["seeds"].items() if int(key) > revision - 5
        }
        repo.import_document("seeds", state)
        assert repo.get_document("seeds") == state
        for key, expected in state["_meta"]["seeds"].items():
            assert repo.get_seed("seeds", key) == expected
    with repo.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM seed_deltas").fetchone()[0] == 4
    state["_meta"]["seeds"] = {}
    repo.import_document("seeds", state)
    assert repo.get_document("seeds") == state
