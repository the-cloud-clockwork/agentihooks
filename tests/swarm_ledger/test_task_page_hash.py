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


@pytest.mark.parametrize("value", [{"b": [True, None, 1.5], "a": "é\n"}, [], "", 42])
def test_streamed_revision_keeps_canonical_digest(value):
    from scripts.swarm_ledger.api.resources import revision

    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert revision(value) == hashlib.sha256(canonical).hexdigest()
