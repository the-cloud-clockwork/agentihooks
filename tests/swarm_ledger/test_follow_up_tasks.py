import json
import re
from types import SimpleNamespace

import pytest

from scripts.swarm import phase_state, slice_check
from scripts.swarm_ledger import ledger, ledger_tasks, plan_backfill, plan_packages, plan_ranges

OP = {"op": "task_add", "id": "a", "by": "master", "task": "f1", "title": "Follow up", "lane": "eng", "phase": "p1"}


def ctx():
    return SimpleNamespace(refused=[], record=lambda *a, **k: None)


@pytest.fixture
def anchored(monkeypatch):
    monkeypatch.setattr(plan_ranges, "anchors", lambda doc, phase: ["first"] if phase.get("id") == "p1" else [])
    return {"phases": [{"id": "p1"}, {"id": "p2"}], "tasks": []}


def test_a_follow_up_task_is_added_to_an_anchored_phase_without_a_slice(anchored):
    done = ctx()
    assert ledger_tasks._add(anchored, {**OP, "follow_up": True}, done) is True
    assert done.refused == []
    task = anchored["tasks"][0]
    assert (task["phase"], task["follow_up"]) == ("p1", True)
    assert not {"slice", "plan_slice", "plan_lines"} & set(task)


def test_a_task_with_no_mark_and_no_slice_is_still_refused(anchored):
    refused = ctx()
    assert ledger_tasks._add(anchored, dict(OP), refused) is False
    assert refused.refused[0].startswith("phase p1 has a plan with slice anchors")
    assert anchored["tasks"] == []


@pytest.mark.parametrize("named", [{"plan_slice": "first"}, {"slice": "slices/plan-a.first"}])
def test_a_follow_up_task_naming_a_slice_is_refused(anchored, named):
    refused = ctx()
    assert ledger_tasks._add(anchored, {**OP, "follow_up": True, **named}, refused) is False
    assert refused.refused == ["task f1 is a follow up and names no slice: drop --plan-slice or --follow-up"]
    assert anchored["tasks"] == []


def test_an_unmarked_task_carries_no_follow_up_field(anchored):
    assert ledger_tasks._add(anchored, {**OP, "phase": "p2"}, ctx()) is True
    assert "follow_up" not in anchored["tasks"][0]


def test_the_follow_up_mark_must_be_true_or_false():
    with pytest.raises(ValueError) as raised:
        ledger_tasks.check_bools({"follow_up": "yes"})
    assert str(raised.value) == "follow_up must be true or false"


@pytest.mark.parametrize(("argv", "expected"), [(["--follow-up"], {"follow_up": True}), ([], {})])
def test_task_add_sends_the_follow_up_mark_only_when_given(monkeypatch, argv, expected):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, op, **fields: sent.append((op, fields)))
    args = ledger.build_parser().parse_args(["--slug", "s", "--as", "master", "task", "add", "f1", "Fix", *argv])
    ledger.cmd_task(args)
    op, fields = sent[0]
    assert op == "task_add"
    assert fields.get("follow_up") == expected.get("follow_up")


def test_the_task_add_schema_takes_a_boolean_follow_up_mark():
    from scripts.swarm_ledger.api import schemas
    from scripts.swarm_ledger.api.errors import APIError

    schema = schemas.operation_schema("task_add")
    assert schemas.validate(schema, {**OP, "follow_up": True}) is None
    with pytest.raises(APIError) as error:
        schemas.validate(schema, {**OP, "follow_up": "yes"})
    assert (error.value.status, error.value.code) == (400, "schema_invalid")


def test_a_follow_up_task_lists_under_its_phase(anchored):
    anchored["phases"][0].update(planning="auto", review={"state": "pending"})
    assert ledger_tasks._add(anchored, {**OP, "follow_up": True}, ctx()) is True
    anchored["tasks"].append({"id": "plan-p1", "phase": "p1", "kind": "plan", "state": "done"})
    assert phase_state.report(anchored)[0] == ("p1", "in_review", ["f1"])


def test_a_follow_up_task_linking_the_plan_needs_no_plan_lines():
    follow_up = {"id": "f1", "phase": "p1", "plan_url": "https://example.com/plan", "follow_up": True}
    unmarked = {**follow_up, "id": "t2", "follow_up": False}
    doc = {"phases": [{"id": "p1"}], "tasks": [follow_up, unmarked]}
    assert slice_check._unlined({"id": "p1"}, doc) == ["Task t2 links the plan but has no plan lines."]


def test_backfill_gives_a_follow_up_task_no_slice(monkeypatch, capsys):
    url = "https://example.com/plan"
    doc = {"tasks": [{"id": "f1", "plan_url": url, "follow_up": True}, {"id": "t2", "plan_url": url}]}

    def unnamed(task):
        raise ValueError(f"no package for {task['id']}")

    monkeypatch.setattr(ledger, "call", lambda slug: doc)
    monkeypatch.setattr(ledger, "send", lambda *args, **kwargs: pytest.fail("a follow up task takes no slice"))
    monkeypatch.setattr(plan_packages, "name", unnamed)
    plan_backfill.run(SimpleNamespace(slug="proof"))
    assert json.loads(capsys.readouterr().out) == {
        "updated": [],
        "missing": [{"task": "t2", "reason": "no package for t2"}],
    }


def test_task_add_help_names_the_follow_up_mark(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "400")
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["--slug", "s", "--as", "master", "task", "add", "--help"])
    pattern = r"\s--follow-up\s+a follow up task: no slice in a sliced phase, judged by its text\n"
    assert re.search(pattern, capsys.readouterr().out)


SLICED = {
    "id": "t1",
    "title": "Borrowed",
    "description": "Fix the stale claim notice",
    "phase": "p1",
    "plan_url": "https://example.com/plan",
    "plan_slice": "first",
    "plan_lines": "3-5",
    "slice": "slices/plan-a.first",
}


@pytest.fixture
def sliced():
    return {
        "overview": "Plan hierarchy",
        "phases": [{"id": "p1", "title": "Phase one", "description": "Slices"}],
        "slices": [{"id": "plan-a.first", "anchor": "first", "lines": "3-5", "phase": "phases/p1"}],
        "tasks": [dict(SLICED)],
    }


def update_ctx():
    return SimpleNamespace(refused=[], record=lambda *a, **k: None, stamp=lambda *a: None, meta={}, dirty=False)


def task_set(fields):
    op = {"op": "task_update", "by": "master", "item": "tasks/t1", "fields": fields}
    ledger_tasks.check(op)
    return op


@pytest.mark.parametrize("fields", [{"follow_up": True}, {"follow_up": True, "plan_slice": ""}])
def test_task_set_marks_a_sliced_task_a_follow_up_and_clears_its_slice(sliced, fields):
    done = update_ctx()
    assert ledger_tasks.apply(sliced, task_set(fields), done) is True
    assert done.refused == []
    task = sliced["tasks"][0]
    assert {key: task[key] for key in ("follow_up", "plan_slice", "plan_lines", "slice")} == {
        "follow_up": True,
        "plan_slice": "",
        "plan_lines": "",
        "slice": "",
    }
    assert (task["plan_url"], task["description"]) == (SLICED["plan_url"], SLICED["description"])


def test_the_intent_state_of_a_task_set_follow_up_carries_its_description_alone(sliced, monkeypatch):
    from scripts.gates import intent
    from scripts.swarm_ledger import plan_read

    monkeypatch.setattr(plan_read, "exact", lambda doc, task: "borrowed plan lines\n")
    pr = {"title": "Fix it", "body": "Closes 1", "files": ["scripts/x.py"]}
    before = intent.state_of(sliced, sliced["tasks"][0], pr)
    assert (before["plan_lines"], before["plan_chunk"]) == ("3-5", "borrowed plan lines\n")
    assert ledger_tasks.apply(sliced, task_set({"follow_up": True}), update_ctx()) is True
    state = intent.state_of(sliced, sliced["tasks"][0], pr)
    assert not {"plan_lines", "plan_chunk"} & set(state)
    assert state["task_text"] == SLICED["description"]


@pytest.mark.parametrize("named", [{"plan_slice": "first"}, {"slice": "slices/plan-a.first"}])
def test_task_set_refuses_a_follow_up_that_names_a_slice(sliced, named):
    refused = update_ctx()
    assert ledger_tasks.apply(sliced, task_set({"follow_up": True, **named}), refused) is False
    assert refused.refused == ["task t1 is a follow up and names no slice: drop plan_slice or follow_up"]
    assert sliced["tasks"][0] == SLICED


def test_task_set_unmarking_a_follow_up_keeps_its_slice(sliced):
    assert ledger_tasks.apply(sliced, task_set({"follow_up": False}), update_ctx()) is True
    assert sliced["tasks"][0] == {**SLICED, "follow_up": False}


@pytest.mark.parametrize(("value", "expected"), [("yes", True), ("no", False)])
def test_task_set_sends_the_follow_up_mark_as_a_boolean(monkeypatch, value, expected):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, op, **fields: sent.append((op, fields)))
    argv = ["--slug", "s", "--as", "master", "task", "set", "t1", f"follow_up={value}"]
    ledger.cmd_task(ledger.build_parser().parse_args(argv))
    assert sent == [("task_update", {"item": "tasks/t1", "fields": {"follow_up": expected}})]


def test_task_set_refuses_a_follow_up_mark_other_than_yes_or_no(monkeypatch):
    monkeypatch.setattr(ledger, "send", lambda *a, **k: pytest.fail("nothing is sent"))
    argv = ["--slug", "s", "--as", "master", "task", "set", "t1", "follow_up=true"]
    with pytest.raises(SystemExit) as refused:
        ledger.cmd_task(ledger.build_parser().parse_args(argv))
    assert str(refused.value) == "task set takes follow_up=yes or follow_up=no"
