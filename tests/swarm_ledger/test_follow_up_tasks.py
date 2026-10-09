from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger, ledger_tasks, plan_ranges

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
