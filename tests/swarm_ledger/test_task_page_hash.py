import hashlib
import json
import tracemalloc

import pytest

pytestmark = pytest.mark.unit


def test_task_page_hash_tracks_only_returned_content():
    from scripts.swarm_ledger.api import resources

    doc = {"tasks": [{"id": "t1", "title": "Visible"}, {"id": "t2", "title": "Outside"}], "_meta": {"rev": 1}}
    first = resources.read(doc, "tasks", {"limit": 1})
    assert first["data"][0]["title"] == "Visible"
    assert resources.read(doc, "tasks", {"limit": 1}) == first
    doc["tasks"][1]["title"] = "Changed outside"
    doc["_meta"]["rev"] = 2
    outside = resources.read(doc, "tasks", {"limit": 1})
    assert outside["data"] == first["data"]
    assert outside["page_hash"] == first["page_hash"]
    assert outside["revision"] != first["revision"]
    with pytest.raises(resources.APIError, match="Collection changed"):
        resources.read(doc, "tasks", {"limit": 1, "cursor": first["next_cursor"]})
    doc["tasks"][0]["title"] = "Changed visible"
    doc["_meta"]["rev"] = 3
    visible = resources.read(doc, "tasks", {"limit": 1})
    assert visible["page_hash"] != first["page_hash"]
    canonical = json.dumps(visible["data"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert visible["page_hash"] == hashlib.sha256(canonical).hexdigest()


def test_task_page_peak_allocation_is_bounded_by_page():
    from scripts.swarm_ledger.api import resources

    doc = {"tasks": [{"id": f"t{index}", "title": "é" * 4096} for index in range(2000)]}
    tracemalloc.start()
    try:
        reply = resources.read(doc, "tasks", {"limit": 2})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(reply["data"]) == 2
    assert peak < 512_000, f"page request allocated {peak} bytes for two returned tasks"


def test_task_page_hash_counts_toward_reply_budget(monkeypatch):
    from scripts.swarm_ledger.api import resources

    doc = {"tasks": [{"id": "t1", "title": "Visible"}, {"id": "t2", "title": "Next"}]}
    full = resources.read(doc, "tasks", {"limit": 2})
    monkeypatch.setattr(resources, "MAX_REPLY", resources.reply_size(full) - 1)
    first = resources.read(doc, "tasks", {"limit": 2})
    assert len(first["data"]) == 1
    assert resources.reply_size(first) <= resources.MAX_REPLY
    second = resources.read(doc, "tasks", {"limit": 2, "cursor": first["next_cursor"]})
    assert second["data"][0]["id"] == "t2"
    assert second["next_cursor"] is None
    empty = resources.read({"tasks": []}, "tasks")
    assert empty["page_hash"] == hashlib.sha256(b"[]").hexdigest()


@pytest.mark.parametrize("value", [{"b": [True, None, 1.5], "a": "é\n"}, [], "", 42])
def test_streamed_revision_keeps_canonical_digest(value):
    from scripts.swarm_ledger.api.resources import revision

    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert revision(value) == hashlib.sha256(canonical).hexdigest()


def test_task_route_reuses_entry_revision(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import resources, routes
    from scripts.swarm_ledger.repository.sqlite import Entry
    from tests.swarm_ledger.api_fakes import as_services, task_rows

    doc = {"tasks": [{"title": "Visible é", "id": "t1"}, {"id": "t2", "title": "Outside"}], "_meta": {"rev": 7}}
    expected = f"{7:064x}"
    entry = Entry(1, "", doc)
    server = as_services(SimpleNamespace(repository=SimpleNamespace(task_rows=task_rows(lambda: entry))))
    original = resources.revision

    def refuse_collection_hash(value):
        assert value is not entry.state["tasks"]
        return original(value)

    monkeypatch.setattr(resources, "revision", refuse_collection_hash)
    first = routes.ledger_read(server, "ledger", "tasks", {"limit": 1})
    assert first["revision"] == expected
    assert routes.ledger_read(server, "ledger", "tasks", {"limit": 1}) == first
    doc = {
        "tasks": [{"title": "Visible é", "id": "t1"}, {"id": "t2", "title": "Changed"}],
        "_meta": {"rev": 9, "task_revision": 8},
    }
    entry = Entry(2, "", doc)
    changed = routes.ledger_read(server, "ledger", "tasks", {"limit": 1})
    assert changed["revision"] == f"{8:064x}"
    assert changed["revision"] != expected
    assert changed["page_hash"] == first["page_hash"]
    with pytest.raises(resources.APIError, match="Collection changed"):
        routes.ledger_read(server, "ledger", "tasks", {"limit": 1, "cursor": first["next_cursor"]})


def test_task_route_peak_allocation_is_bounded_by_page():
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import routes
    from scripts.swarm_ledger.repository.sqlite import Entry
    from tests.swarm_ledger.api_fakes import as_services, task_rows

    doc = {"tasks": [{"id": f"t{index}", "title": "é" * 4096} for index in range(2000)]}
    entry = Entry(1, "", doc)
    server = as_services(SimpleNamespace(repository=SimpleNamespace(task_rows=task_rows(lambda: entry))))
    tracemalloc.start()
    try:
        reply = routes.ledger_read(server, "ledger", "tasks", {"limit": 2})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(reply["data"]) == 2
    assert peak < 512_000, f"page request allocated {peak} bytes for two returned tasks"


@pytest.mark.parametrize("saved", [None, 3])
@pytest.mark.parametrize(
    "before,after,changed",
    [
        (None, None, False),
        (None, [], False),
        ([], None, False),
        ([], [], False),
        ([{"id": "t1"}], [{"id": "t1"}], False),
        ([], [{"id": "t1"}], True),
    ],
)
def test_task_revision_persists_across_optional_collection_writes(tmp_path, saved, before, after, changed):
    from scripts.swarm_ledger.api.resources import resource_revision
    from tests.swarm_ledger.test_sqlite import document, store

    source = document()
    source.pop("tasks")
    if before is not None:
        source["tasks"] = before
    source["_meta"]["rev"] = 5
    if saved is not None:
        source["_meta"]["task_revision"] = saved
    repository = store(tmp_path)
    repository.import_document("ledger", source)
    entry = repository.current("ledger")
    state = json.loads(entry.text)
    state.pop("tasks", None)
    if after is not None:
        state["tasks"] = after
    state["title"] = "Unrelated title update"
    state["_meta"]["rev"] = 6
    with repository.connect() as connection, connection:
        repository._write(connection, "ledger", entry, state, [])
    reloaded = store(tmp_path).current("ledger").state
    expected = 6 if changed else (5 if saved is None else saved)
    assert reloaded["_meta"]["rev"] == 6
    assert resource_revision(reloaded, "tasks") == f"{expected:064x}"
    assert reloaded.get("tasks", []) == ([] if after is None else after)
