import json

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger.events import patch
from scripts.swarm_ledger.events.hub import Hub
from scripts.swarm_ledger.repository.fragments import Fragment
from scripts.swarm_ledger.repository.rows import encode
from scripts.swarm_ledger.repository.sqlite import Entry, SQLiteLedgerRepository
from tests.swarm_ledger.test_fragment_encoding import held
from tests.swarm_ledger.test_operation_snapshot import SLUG, message, repository
from tests.swarm_ledger.test_operations_cpu_contract import small_corpus


def test_unchanged_collections_share_read_only_generations(tmp_path, monkeypatch):
    stored = repository(tmp_path, monkeypatch)
    state = small_corpus()["state"]
    state["tasks"] = [{"id": "first", "title": "é𝄞", "value": -0.0}]
    stored.import_document(SLUG, state, replace=True)
    old = stored.current(SLUG)
    new, _ = stored.apply_ops(SLUG, ops=[message("chat")], copy_state=False)
    assert new["tasks"] is old.state["tasks"]
    assert new["_meta"] is not old.state["_meta"]
    assert old.state["chat"] == []
    assert stored.current(SLUG).text == encode(new)
    later, _ = stored.apply_ops(SLUG, ops=[message("later")], copy_state=False)
    assert later["tasks"] is new["tasks"]
    assert stored.current(SLUG).text == encode(later)
    detached = stored.get_document(SLUG)
    detached["tasks"][0]["title"] = "Caller edit"
    assert old.state["tasks"][0]["title"] == "é𝄞"


def test_a_generation_joins_its_text_when_read_and_keeps_no_copy(tmp_path, monkeypatch):
    stored = repository(tmp_path, monkeypatch)
    new, _ = stored.apply_ops(SLUG, ops=[message("chat")], copy_state=False)
    entry = stored.current(SLUG)
    assert [item for item in held(entry.fragments) if isinstance(item.signature, str)] == []
    assert Fragment.__slots__ == ("value", "parts", "signature")
    assert entry.text == encode(new)
    assert Entry(1, "stored text", {}).text == "stored text"


def test_snapshot_publication_reuses_immutable_resources_and_patches(tmp_path, monkeypatch):
    stored = repository(tmp_path, monkeypatch)
    hub = Hub()
    monkeypatch.setattr(server, "HUB", hub)
    old = stored.current(SLUG).state
    hub.open(SLUG, lambda: {"ledger": server.ledger_view(old)})
    first, _ = stored.apply_ops(SLUG, ops=[message("first")], copy_state=False)
    server.publish_snapshot(SLUG, first)
    second, _ = stored.apply_ops(SLUG, ops=[message("second")], copy_state=False)
    server.publish_snapshot(SLUG, second)
    assert hub.resource(SLUG, "ledger")["chat"] is second["chat"]
    assert first["chat"] is not second["chat"]
    rebuilt = server.ledger_view(old)
    for _, _, _, data in hub.wait(SLUG, 0, 0):
        rebuilt = patch.apply(rebuilt, data["patch"])
    assert patch.same(rebuilt, server.ledger_view(second))
    assert [item["id"] for item in first["chat"]] == ["first"]


@pytest.mark.parametrize("reload", ["import", "rebuild", "other_writer"])
def test_generation_fragments_follow_every_replacement_write(tmp_path, monkeypatch, reload):
    stored = repository(tmp_path, monkeypatch)
    before = stored.current(SLUG)
    if reload == "rebuild":
        stored.apply_ops(SLUG, ops=[message("before")])
        stored.export_document(SLUG)
        stored.rebuild(SLUG)
    elif reload == "other_writer":
        other = SQLiteLedgerRepository(stored.path, domain=server.core)
        other.apply_ops(SLUG, ops=[message("before")])
    else:
        state = small_corpus()["state"]
        state["title"] = "Replaced"
        stored.import_document(SLUG, state, replace=True)
    latest = stored.current(SLUG)
    assert latest is not before
    result, _ = stored.apply_ops(SLUG, ops=[message("after")], copy_state=False)
    assert result["title"] == latest.state["title"]
    assert result["chat"][-1]["id"] == "after"
    assert json.loads(stored.current(SLUG).text) == result
