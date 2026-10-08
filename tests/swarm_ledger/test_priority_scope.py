import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_priorities, new_ledger

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
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
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
