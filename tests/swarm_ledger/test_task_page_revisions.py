import pytest

from scripts.swarm_ledger.api import resources
from scripts.swarm_ledger.api.errors import APIError


@pytest.mark.parametrize("limit", [1, 10, 100])
def test_task_page_hashes_only_selected_tasks(monkeypatch, limit):
    rows = [{"id": f"t{n}", "title": f"Task {n}", "comments": [{"text": "Measured write"}]} for n in range(1000)]
    doc = {"tasks": rows, "_meta": {"task_revision": 7}}
    query = {"limit": limit, "cursor": f"{7:064x}:100"}
    expected = resources.read(doc, "tasks", query)
    encoded = []
    dumps = resources.json.dumps
    identities = {id(row) for row in rows}

    def measured_dumps(value, *args, **kwargs):
        if id(value) in identities:
            encoded.append(value["id"])
        return dumps(value, *args, **kwargs)

    monkeypatch.setattr(resources.json, "dumps", measured_dumps)

    reply = resources.read(doc, "tasks", query, {})

    assert reply == expected
    assert reply["next_cursor"] == f"{7:064x}:{100 + limit}"
    assert encoded == [f"t{n}" for n in range(100, 100 + limit)]


def test_task_page_revisions_follow_comments_and_reject_old_cursors():
    rows = [
        {"id": "t1", "title": "First", "comments": [{"text": "Before"}]},
        {"id": "t2", "title": "Second", "comments": []},
    ]
    doc = {"tasks": rows, "_meta": {"task_revision": 7}}
    before = resources.read(doc, "tasks", {"limit": 1}, {})
    rows[0]["comments"].append({"text": "After"})
    doc["_meta"]["task_revision"] = 8

    after = resources.read(doc, "tasks", {"limit": 1}, {})

    assert before["data"][0]["comments_count"] == 1
    assert after["data"][0]["comments_count"] == 2
    assert after["data"][0]["revision"] == resources.revision(rows[0])
    assert before["data"][0]["revision"] != after["data"][0]["revision"]
    assert before["page_hash"] != after["page_hash"]
    with pytest.raises(APIError) as error:
        resources.read(doc, "tasks", {"limit": 1, "cursor": before["next_cursor"]}, {})
    assert error.value.status == 409
