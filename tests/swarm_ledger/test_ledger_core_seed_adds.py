import json

import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import new_ledger

SLUG = "seed-adds-proof"
CONTENT = {
    "title": "Demo",
    "phases": [{"title": "First"}],
    "questions": [{"text": "which disk?"}],
    "followups": [{"text": "check the disk"}],
    "tasks": [{"id": "t1", "title": "Build it", "phase": "p1", "lane": "eng"}],
}


@pytest.fixture(autouse=True)
def seed_ledger_dir(ledger_dir, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", ledger_dir)


def make_ledger():
    html, state = core.paths(SLUG)
    html.write_text(new_ledger.render(new_ledger.build_doc(CONTENT), SLUG, 8765))
    state.unlink(missing_ok=True)
    return core.sync(SLUG)[0]


def edit_seed(change):
    html, _ = core.paths(SLUG)
    source = html.read_text()
    seed = core.parse_seed(source)
    change(seed)
    html.write_text(core.SEED_RE.sub(lambda m: m[1] + json.dumps(seed) + m[3], source))
    return core.sync(SLUG)[0]


def page_seed():
    return core.parse_seed(core.paths(SLUG)[0].read_text())


def test_a_ledger_created_from_a_seed_page_keeps_every_item():
    state = make_ledger()
    assert [p["title"] for p in state["phases"]] == ["First"]
    assert [t["title"] for t in state["tasks"]] == ["Build it"]
    assert [f["text"] for f in state["followups"]] == ["check the disk"]
    assert [q["text"] for q in state["questions"]] == ["which disk?"]
    assert state["_meta"]["warnings"] == []


def test_a_new_task_in_the_seed_page_is_refused_with_its_add_command():
    before = make_ledger()
    state = edit_seed(lambda seed: seed["tasks"].append({"id": "t2", "title": "Ship it", "phase": "p1", "lane": "eng"}))
    assert state["tasks"] == before["tasks"]
    assert state["_meta"]["warnings"] == [
        'The page added the task "Ship it", which was not added. Add it with '
        'agentihooks ledger --slug <slug> --as <name> task add <id> "Ship it", which checks it for duplicates.'
    ]
    assert not [e for e in state["_meta"]["events"] if e["kind"] == "added"]
    assert [t["id"] for t in page_seed()["tasks"]] == ["t1"]


def test_a_new_follow_up_in_the_seed_page_is_refused_with_its_add_command():
    before = make_ledger()
    state = edit_seed(lambda seed: seed["followups"].append({"id": "f2", "text": "rotate the logs"}))
    assert state["followups"] == before["followups"]
    assert state["_meta"]["warnings"] == [
        'The page added the follow up "rotate the logs", which was not added. Add it with '
        'agentihooks ledger --slug <slug> --as <name> followup add "rotate the logs", which checks it for duplicates.'
    ]


def test_a_new_phase_in_the_seed_page_is_refused_with_its_add_command():
    before = make_ledger()
    state = edit_seed(lambda seed: seed["phases"].append({"id": "p2", "title": "Second", "depends_on": ["p1"]}))
    assert state["phases"] == before["phases"]
    assert state["_meta"]["warnings"] == [
        'The page added the phase "Second", which was not added. Add it with '
        'agentihooks ledger --slug <slug> --as <name> phase add <id> "Second", which checks it for duplicates.'
    ]
    assert state["_meta"]["seed_error"] is None


def test_an_existing_phase_cannot_depend_on_a_refused_new_phase():
    before = make_ledger()

    def change(seed):
        seed["phases"].append({"id": "p2", "title": "Second"})
        seed["phases"][0]["depends_on"] = ["p2"]

    state = edit_seed(change)
    assert state["phases"] == before["phases"]


def test_an_edited_title_still_merges():
    make_ledger()

    def change(seed):
        seed["tasks"][0]["title"] = "Build it well"
        seed["phases"][0]["title"] = "Opening"

    state = edit_seed(change)
    assert state["tasks"][0]["title"] == "Build it well"
    assert state["phases"][0]["title"] == "Opening"
    assert state["_meta"]["warnings"] == []


def test_a_new_question_still_merges():
    make_ledger()
    state = edit_seed(lambda seed: seed["questions"].append({"id": "q2", "text": "which region?"}))
    assert [q["text"] for q in state["questions"]] == ["which disk?", "which region?"]
    assert [e["target"] for e in state["_meta"]["events"] if e["kind"] == "added"] == ["questions/q2"]
    assert state["_meta"]["warnings"] == []


def test_a_new_comment_on_an_existing_task_still_merges():
    make_ledger()
    state = edit_seed(
        lambda seed: seed["tasks"][0]["comments"].append(
            {"id": "c-new", "by": "engineer", "text": "Started the build."}
        )
    )
    assert [c["text"] for c in state["tasks"][0]["comments"]] == ["Started the build."]


def test_an_item_added_by_a_command_after_the_page_copy_is_not_refused():
    make_ledger()
    html, _ = core.paths(SLUG)
    stale = html.read_text()
    op = {"op": "add_item", "id": "add-1", "by": "engineer", "list": "followups", "text": "rotate the logs"}
    core.check_op(op)
    added = core.sync(SLUG, ops=[op])[0]["followups"][-1]
    seed = core.parse_seed(stale)
    seed["followups"].append(added)
    seed["title"] = "Demo two"
    html.write_text(core.SEED_RE.sub(lambda m: m[1] + json.dumps(seed) + m[3], stale))
    state = core.sync(SLUG)[0]
    assert state["title"] == "Demo two"
    assert [f["text"] for f in state["followups"]] == ["check the disk", "rotate the logs"]
    assert state["_meta"]["warnings"] == []
