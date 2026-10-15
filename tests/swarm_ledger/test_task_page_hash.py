import hashlib
import json
import tracemalloc

import pytest

pytestmark = pytest.mark.unit


def test_task_page_hash_tracks_only_returned_content():
    from scripts.swarm_ledger.api import resources

    doc = {"tasks": [{"id": "t1", "title": "Visible"}, {"id": "t2", "title": "Outside"}]}
    first = resources.read(doc, "tasks", {"limit": 1})
    assert first["data"][0]["title"] == "Visible"
    assert resources.read(doc, "tasks", {"limit": 1}) == first
    doc["tasks"][1]["title"] = "Changed outside"
    outside = resources.read(doc, "tasks", {"limit": 1})
    assert outside["data"] == first["data"]
    assert outside["page_hash"] == first["page_hash"]
    assert outside["revision"] != first["revision"]
    with pytest.raises(resources.APIError, match="Collection changed"):
        resources.read(doc, "tasks", {"limit": 1, "cursor": first["next_cursor"]})
    doc["tasks"][0]["title"] = "Changed visible"
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

    doc = {"tasks": [{"title": "Visible é", "id": "t1"}, {"id": "t2", "title": "Outside"}]}
    expected = resources.revision(doc["tasks"])
    entry = Entry(1, "", doc)
    server = SimpleNamespace(repository=SimpleNamespace(current=lambda slug: entry))

    def refuse_collection_hash(doc, path):
        pytest.fail(f"Task page read recomputed {path} collection revision")

    monkeypatch.setattr(resources, "resource_revision", refuse_collection_hash)
    first = routes.ledger_read(server, "ledger", "tasks", {"limit": 1})
    assert first["revision"] == expected
    assert routes.ledger_read(server, "ledger", "tasks", {"limit": 1}) == first
    doc = {"tasks": [{"title": "Visible é", "id": "t1"}, {"id": "t2", "title": "Changed"}]}
    entry = Entry(2, "", doc)
    changed = routes.ledger_read(server, "ledger", "tasks", {"limit": 1})
    assert changed["revision"] == resources.revision(doc["tasks"])
    assert changed["revision"] != expected
    assert changed["page_hash"] == first["page_hash"]
    with pytest.raises(resources.APIError, match="Collection changed"):
        routes.ledger_read(server, "ledger", "tasks", {"limit": 1, "cursor": first["next_cursor"]})


def test_task_route_peak_allocation_is_bounded_by_page():
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import routes
    from scripts.swarm_ledger.repository.sqlite import Entry

    doc = {"tasks": [{"id": f"t{index}", "title": "é" * 4096} for index in range(2000)]}
    entry = Entry(1, "", doc)
    server = SimpleNamespace(repository=SimpleNamespace(current=lambda slug: entry))
    tracemalloc.start()
    try:
        reply = routes.ledger_read(server, "ledger", "tasks", {"limit": 2})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(reply["data"]) == 2
    assert peak < 512_000, f"page request allocated {peak} bytes for two returned tasks"
