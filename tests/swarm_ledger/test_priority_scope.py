import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_priorities, new_ledger
from tests.swarm_ledger import legacy_page

SLUG = "priority-scope"


@pytest.fixture
def state():
    content = {
        "title": "Demo",
        "overview": "Priority scope",
        "sources": [],
        "phases": [{"title": "one", "description": "d"}],
        "questions": [{"text": "Which option?"}],
        "followups": [{"text": "Check the disk"}],
        "tasks": [{"title": "Wire the broker", "phase": "p1", "lane": "eng"}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    return core.sync(SLUG, ops=[{"op": "join", "id": "join", "by": "boss", "role": "orchestrator"}])[0]


@pytest.mark.parametrize("name", ["phases", "questions", "followups", "tasks"])
def test_priority_add_refuses_out_of_scope_item_with_reason(state, name):
    item = f"{name}/{state[name][0]['id']}"
    state, rejected = core.sync(SLUG, changes=[{"path": f"{item}/out_of_scope", "value": True}])
    assert rejected == []
    op = {"op": "priority", "id": "priority", "by": "boss", "item": item, "text": "Approve this choice."}
    ledger_priorities.check(op)
    state, rejected = core.sync(SLUG, ops=[op])
    assert rejected == ["priority"]
    assert state["_meta"]["warnings"] == ["Cannot add a priority to an out of scope item."]
    assert item not in [row["item"] for row in state["priorities"]]
    assert not any(event["kind"] == "priority added" for event in state["_meta"]["events"])


@pytest.mark.parametrize("name", ["phases", "questions", "followups", "tasks"])
def test_existing_priority_disappears_when_item_moves_out_of_scope(state, name):
    item = f"{name}/{state[name][0]['id']}"
    other = f"phases/{state['phases'][0]['id']}" if name != "phases" else f"tasks/{state['tasks'][0]['id']}"
    ops = [
        {"op": "priority", "id": "priority", "by": "boss", "item": item, "text": "Approve this choice."},
        {"op": "priority", "id": "other", "by": "boss", "item": other, "text": "Keep this choice."},
    ]
    state, rejected = core.sync(SLUG, ops=ops)
    assert rejected == []
    assert item in [row["item"] for row in state["priorities"]]
    state, rejected = core.sync(SLUG, changes=[{"path": f"{item}/out_of_scope", "value": True}])
    assert rejected == []
    assert item not in [row["item"] for row in state["priorities"]]
    assert other in [row["item"] for row in state["priorities"]]
    state, _ = core.sync(SLUG)
    assert item not in [row["item"] for row in state["priorities"]]


@pytest.mark.parametrize("name", ["phases", "questions", "followups", "tasks"])
def test_priority_extension_refuses_out_of_scope_target(name):
    doc = {name: [{"id": "choice", "out_of_scope": True}]}
    ctx = core.Context({"rev": 0, "stamps": {}}, 1)
    op = {"op": "priority", "id": "priority", "by": "boss", "item": f"{name}/choice", "text": "Approve this choice."}
    assert ledger_priorities.apply(doc, op, ctx) is False
    assert ctx.refused == ["Cannot add a priority to an out of scope item."]
    assert "priorities" not in doc
    assert ctx.events == []


def test_priority_extension_refuses_missing_collection():
    ctx = core.Context({"rev": 0, "stamps": {}}, 1)
    op = {"op": "priority", "id": "priority", "by": "boss", "item": "tasks/missing", "text": "Approve this choice."}
    assert ledger_priorities.apply({"phases": []}, op, ctx) is False
    assert ctx.refused == []
    assert ctx.events == []


@pytest.mark.parametrize("name", ["phases", "questions", "followups", "tasks"])
def test_priority_derivation_filters_out_of_scope_targets_in_sparse_document(name):
    kept = {"id": "keep", "item": f"{name}/keep", "text": "Keep this choice."}
    doc = {
        "phases": [],
        name: [{"id": "choice", "out_of_scope": True}, {"id": "keep"}],
        "priorities": [{"id": "stale", "item": f"{name}/choice", "text": "Approve this choice."}, kept],
    }
    if name == "questions":
        doc[name][1]["answers"] = [{"text": "Already answered."}]
    ctx = core.Context({"rev": 0, "stamps": {}}, 1)
    ledger_priorities.derive(doc, ctx)
    assert doc["priorities"] == [kept]
    assert ctx.dirty is True
    assert ctx.events == [
        {
            "rev": 1,
            "at": 1,
            "by": "swarm",
            "kind": "priority cleared",
            "target": f"{name}/choice",
            "id": "stale",
            "reason": "its item is out of scope",
        }
    ]
