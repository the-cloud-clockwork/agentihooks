from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger.events.publishing import Publishing
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.test_operations_cpu_contract import small_corpus

SLUG = "snapshot-proof"


def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server.core, "now_ms", lambda: 1791939600000)
    stored = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3", domain=server.core)
    stored.import_document(SLUG, small_corpus()["state"])
    return stored


def message(identity):
    return {"op": "add", "id": identity, "thread": "chat", "text": "Recorded message"}


def test_default_reads_and_mutation_results_remain_detached(tmp_path, monkeypatch):
    stored = repository(tmp_path, monkeypatch)
    state, rejected = stored.apply_ops(SLUG, ops=[message("first")])
    assert rejected == []
    state["chat"][0]["text"] = "Caller edit"
    read = stored.get_document(SLUG)
    assert read["chat"][0]["text"] == "Recorded message"
    read["title"] = "Caller title"
    assert stored.get_document(SLUG)["title"] == "Replay"


def test_borrowed_result_keeps_its_own_committed_generation(tmp_path, monkeypatch):
    stored = repository(tmp_path, monkeypatch)
    previous = stored.current(SLUG).state
    first, rejected = stored.apply_ops(SLUG, ops=[message("first")], copy_state=False)
    assert rejected == []
    assert first["tasks"] is previous["tasks"]
    assert first is stored.current(SLUG).state
    revision = first["_meta"]["rev"]
    stored.apply_ops(SLUG, ops=[message("second")])
    assert [row["id"] for row in first["chat"]] == ["first"]
    assert first["_meta"]["rev"] == revision
    assert stored.current(SLUG).state["_meta"]["rev"] == revision + 1


def test_publishing_preserves_borrowed_and_detached_result_ownership(tmp_path, monkeypatch):
    stored = repository(tmp_path, monkeypatch)
    publishing = Publishing(stored)
    borrowed, rejected = publishing.apply_ops(SLUG, ops=[message("borrowed")], copy_state=False)
    assert rejected == []
    assert borrowed is stored.current(SLUG).state
    detached, rejected = publishing.apply_ops(SLUG, ops=[message("detached")])
    assert rejected == []
    assert detached is not stored.current(SLUG).state
    detached["chat"][0]["text"] = "Caller edit"
    assert borrowed["chat"][0]["text"] == "Recorded message"
    assert stored.get_document(SLUG)["chat"][0]["text"] == "Recorded message"


def test_publication_of_a_newer_write_keeps_the_original_response(tmp_path, monkeypatch):
    stored = repository(tmp_path, monkeypatch)
    advanced = []

    def publish(slug, state):
        if not advanced:
            advanced.append(True)
            stored.apply_ops(slug, ops=[message("second")])

    publishing = Publishing(stored, publish)
    result, rejected = publishing.apply_ops(SLUG, ops=[message("first")], copy_state=False)
    publishing.drain(5)
    assert rejected == []
    assert [row["id"] for row in result["chat"]] == ["first"]
    assert [row["id"] for row in stored.current(SLUG).state["chat"]] == ["first", "second"]
    assert result["_meta"]["rev"] + 1 == stored.current(SLUG).state["_meta"]["rev"]
