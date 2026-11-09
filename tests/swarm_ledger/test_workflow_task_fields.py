import pytest

from scripts.swarm_ledger import new_ledger
from scripts.swarm_ledger.api import schemas
from tests.swarm_ledger.test_ledger_authority import (
    MASTER,
    SLUG,
    ledger,
    operation,
    pinned,
)
from tests.swarm_ledger.test_ledger_authority import (
    crew as authority_crew,
)
from tests.swarm_ledger.test_ledger_authority import (
    live as authority_live,
)

live = authority_live
crew = authority_crew


@pytest.mark.feature("ledger-workflow-task-fields")
def test_task_add_schema_accepts_workflow_and_case_as_strings() -> None:
    task = operation(
        "task_add", by=MASTER, task="t20", title="Workflow proof", lane="eng", kind="workflow", workflow="review"
    )
    schema = schemas.operation_schema("task_add")
    assert schemas.mismatched_field(schema, task) is None
    assert schemas.mismatched_field(schema, {**task, "case": "/proof/case.json"}) is None
    assert schemas.mismatched_field(schema, {**task, "workflow": 12}) == "workflow"
    assert schemas.mismatched_field(schema, {**task, "case": {}}) == "case"


@pytest.mark.feature("ledger-workflow-task-fields")
def test_a_master_adds_a_workflow_task_through_the_api(crew: dict) -> None:
    added = operation(
        "task_add",
        by=MASTER,
        task="t20",
        title="Workflow proof",
        lane="eng",
        phase="p1",
        kind="workflow",
        workflow="review",
        case="/proof/case.json",
    )
    with pinned(MASTER):
        reply = ledger.request(SLUG, [added])
        assert added["id"] not in reply["rejected"]
        task = next(task for task in ledger.request(SLUG)["doc"]["tasks"] if task["id"] == "t20")
    assert {key: task[key] for key in ("kind", "workflow", "case")} == {
        "kind": "workflow",
        "workflow": "review",
        "case": "/proof/case.json",
    }


@pytest.mark.feature("ledger-workflow-task-fields")
@pytest.mark.parametrize("case", [None, "/proof/case.json"])
def test_initial_content_preserves_workflow_task_fields(case: str | None) -> None:
    task = {"title": "Workflow proof", "kind": "workflow", "workflow": "review"}
    if case is not None:
        task["case"] = case
    content = {"title": "Workflow ledger", "phases": [{"title": "Proof"}], "tasks": [task]}
    assert new_ledger.check(content) == []
    seeded = new_ledger.build_doc(content)["tasks"][0]
    assert seeded["kind"] == "workflow"
    assert seeded["workflow"] == "review"
    assert seeded.get("case") == case
