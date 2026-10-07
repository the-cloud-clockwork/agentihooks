import pytest


def test_file_repository_exposes_storage_contract():
    from scripts.swarm_ledger.repository import FileLedgerRepository, LedgerRepository

    assert isinstance(FileLedgerRepository(), LedgerRepository)
    for method in ("get_document", "apply_ops", "events_since", "list_summaries", "create", "delete", "restore"):
        assert callable(getattr(FileLedgerRepository(), method))


def test_repository_operations_and_bin_round_trip():
    from scripts.swarm_ledger.repository import FileLedgerRepository

    repo = FileLedgerRepository()
    content = {"title": "Repository", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    assert repo.create("repository", content)
    before = repo.get_document("repository")
    state, rejected = repo.apply_ops("repository", ops=[{"op": "join", "id": "join", "by": "eng"}])
    assert rejected == []
    assert "eng" in state["_meta"]["members"]
    assert repo.events_since("repository", before["_meta"]["rev"])
    assert any(row["slug"] == "repository" for row in repo.list_summaries())
    assert not repo.create("repository", content)
    repo.delete("repository", now=123)
    assert repo.restore("repository", now=456)
    assert not repo.restore("repository", now=789)
    assert repo.get_document("repository") == state


def test_missing_document_preserves_error():
    from scripts.swarm_ledger.repository import FileLedgerRepository

    with pytest.raises(FileNotFoundError):
        FileLedgerRepository().get_document("missing")
