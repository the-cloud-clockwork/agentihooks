from copy import deepcopy
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger import ledger_plans

OLD = "http://127.0.0.1:8765/artifacts/ledger/content.md"
NEW = "https://hosted.example/artifacts/ledger/content.md"
ISSUE = "https://github.com/acme/app/issues/1"


def document(url=ISSUE):
    return {
        "plans": [{"id": "plan-content", "title": "Plan", "artifact": OLD, "url": url}],
        "phases": [
            {
                "id": "first",
                "plan": "plans/plan-content",
                "plan_url": url,
                "plan_ref": {"artifact": OLD, "lines": "2-9"},
            },
            {
                "id": "second",
                "plan": "plans/plan-content",
                "plan_url": url,
                "plan_ref": {"artifact": OLD, "lines": "10-19"},
            },
            {
                "id": "other",
                "plan": "plans/another",
                "plan_url": ISSUE,
                "plan_ref": {"artifact": OLD, "lines": "20-21"},
            },
            {"id": "unplanned"},
        ],
        "tasks": [{"id": "task", "slice": "slices/plan-content.build", "plan_lines": "4-8"}],
        "slices": [{"id": "plan-content.build", "phase": "phases/first", "lines": "4-8"}],
    }


@pytest.mark.parametrize("url", [ISSUE, OLD])
def test_repoint_changes_only_server_and_preserves_phase_ranges_and_task_slices(url):
    doc = document(url)
    before = deepcopy(doc)
    ctx = Mock()
    target_url = NEW if url == OLD else url
    op = {
        "op": "plan_add",
        "by": "engineer",
        "plan": "plan-content",
        "title": "Plan",
        "artifact": NEW,
        "url": target_url,
        "repoint": True,
    }
    ledger_plans.check(op)
    assert ledger_plans.add_plan(doc, op, ctx) == ""
    assert doc["plans"] == [{"id": "plan-content", "title": "Plan", "artifact": NEW, "url": target_url}]
    assert doc["phases"][:2] == [
        {**phase, "plan_url": target_url, "plan_ref": {**phase["plan_ref"], "artifact": NEW}}
        for phase in before["phases"][:2]
    ]
    assert doc["phases"][2:] == before["phases"][2:]
    assert doc["tasks"] == before["tasks"]
    assert doc["slices"] == before["slices"]
    ctx.record.assert_called_once_with("engineer", "updated", "plans/plan-content", text=NEW)


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Other"},
        {"url": "https://other.example"},
        {"artifact": "https://hosted.example/artifacts/ledger/different.md"},
        {"artifact": "https://hosted.example/artifacts/other/content.md"},
    ],
)
def test_repoint_refuses_identity_changes_without_mutation(change):
    doc = document()
    before = deepcopy(doc)
    ctx = Mock()
    op = {
        "op": "plan_add",
        "by": "engineer",
        "plan": "plan-content",
        "title": "Plan",
        "artifact": NEW,
        "url": ISSUE,
        "repoint": True,
        **change,
    }
    assert ledger_plans.add_plan(doc, op, ctx) == "repoint may change only the server of the same plan artifact"
    assert doc == before
    ctx.record.assert_not_called()


def test_repoint_requires_existing_plan_and_boolean_flag():
    op = {"op": "plan_add", "by": "engineer", "plan": "plan-content", "title": "Plan", "repoint": True}
    doc = {"plans": []}
    assert ledger_plans.add_plan(doc, op, Mock()) == "repoint needs an existing plan"
    assert doc == {"plans": []}
    with pytest.raises(ValueError, match="repoint must be a boolean"):
        ledger_plans.check({**op, "repoint": "yes"})
