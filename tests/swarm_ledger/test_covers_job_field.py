import pytest

from scripts.swarm_ledger import ledger_tasks
from scripts.swarm_ledger.api import schemas
from tests.swarm_ledger import test_artifact_lifecycle as lifecycle
from tests.swarm_ledger.test_artifact_lifecycle import AGENT, add_task, storage
from tests.swarm_ledger.test_artifact_lifecycle import package_modules as package_modules
from tests.swarm_ledger.test_artifact_lifecycle import slug as slug

JOB = "swarm-image / smoke"


@pytest.mark.feature("promotion-covering-task-holds-cause")
def test_a_task_added_or_set_with_covers_job_keeps_the_job(slug: str) -> None:
    state, _ = add_task(slug, "c1", covers_job=JOB)
    assert next(t for t in state["tasks"] if t["id"] == "c1")["covers_job"] == JOB
    update = {"op": "task_update", "id": "u", "by": AGENT, "item": "tasks/c1", "fields": {"covers_job": "lint"}}
    state, _ = storage.apply_ops(slug, ops=[update])
    assert next(t for t in state["tasks"] if t["id"] == "c1")["covers_job"] == "lint"


@pytest.mark.feature("promotion-covering-task-holds-cause")
def test_covers_job_must_be_a_string() -> None:
    task = {"op": "task_add", "id": "t", "by": "eng", "task": "c", "title": "Work", "lane": "eng"}
    update = {"op": "task_update", "id": "u", "by": "eng", "item": "tasks/c", "fields": {"covers_job": ["lint"]}}
    with pytest.raises(ValueError, match="^covers_job must be a string"):
        ledger_tasks.check({**task, "covers_job": ["lint"]})
    with pytest.raises(ValueError, match="^task_update may set only .*, as strings"):
        ledger_tasks.check(update)


@pytest.mark.feature("promotion-covering-task-holds-cause")
def test_the_task_add_operation_schema_accepts_covers_job() -> None:
    op = {"op": "task_add", "id": "t", "by": "eng", "task": "c", "title": "Work", "lane": "eng", "covers_job": JOB}
    assert schemas.mismatched_field(schemas.operation_schema("task_add"), op) is None


@pytest.mark.feature("promotion-covering-task-holds-cause")
def test_task_add_and_set_mark_the_job_a_task_covers() -> None:
    commands = lifecycle.TestCommands()
    assert commands.run(["task", "add", "c9", "Fix", "--covers-job", JOB])["covers_job"] == JOB
    assert "covers_job" not in commands.run(["task", "add", "c9", "Fix"])
    assert commands.run(["task", "set", "c9", f"covers_job={JOB}"])["fields"] == {"covers_job": JOB}
