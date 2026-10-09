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
