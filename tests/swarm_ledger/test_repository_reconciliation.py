from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.test_repository_files import content


def stored(tmp_path):
    repo = SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    repo.create("reconcile", content())
    return repo


def test_rejected_changes_and_operations_both_survive_the_repository(tmp_path):
    repo = stored(tmp_path)
    _, rejected = repo.apply_ops(
        "reconcile",
        changes=[{"path": "phases/missing/done", "value": True}],
        ops=[{"op": "add", "thread": "phases/missing/comments", "id": "missing", "text": "No thread"}],
    )
    assert rejected == ["phases/missing/done", "missing"]


def test_supplied_gate_can_refuse_an_operation(tmp_path):
    repo = stored(tmp_path)

    class Gate:
        def apply(self, doc, op, ctx, apply):
            return False

    state, rejected = repo.apply_ops(
        "reconcile", ops=[{"op": "add", "thread": "chat", "id": "refused", "text": "No entry"}], gate=Gate()
    )
    assert rejected == ["refused"] and state["chat"] == []
