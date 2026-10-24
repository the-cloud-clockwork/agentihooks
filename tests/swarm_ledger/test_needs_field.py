import ledger_core as core
import pytest

from scripts.swarm_ledger import ledger_tasks
from tests.swarm_ledger import test_artifact_lifecycle as lifecycle
from tests.swarm_ledger.test_artifact_lifecycle import AGENT, add_task, storage
from tests.swarm_ledger.test_artifact_lifecycle import package_modules as package_modules
from tests.swarm_ledger.test_artifact_lifecycle import slug as slug


def test_a_task_added_or_set_with_needs_keeps_the_capabilities(slug):
    state, _ = add_task(slug, "h1", needs=["headed"])
    assert next(t for t in state["tasks"] if t["id"] == "h1")["needs"] == ["headed"]
    update = {"op": "task_update", "id": "u", "by": AGENT, "item": "tasks/h1", "fields": {"needs": []}}
    state, _ = storage.apply_ops(slug, ops=[update])
    assert next(t for t in state["tasks"] if t["id"] == "h1")["needs"] == []


def test_needs_must_be_a_list_of_names():
    assert "needs" in ledger_tasks.UPDATABLE
    task = {"op": "task_add", "id": "t", "by": "eng", "task": "h", "title": "Work", "lane": "eng"}
    update = {"op": "task_update", "id": "u", "by": "eng", "item": "tasks/h", "fields": {"needs": "headed"}}
    for bad in ({**task, "needs": "headed"}, update):
        with pytest.raises(ValueError, match="^needs must be a list"):
            core.check_op(bad)


def run(argv):
    return lifecycle.TestCommands().run(argv)


def test_task_add_needs_marks_the_task():
    assert run(["task", "add", "h9", "Browse", "--needs", "headed, gpu"])["needs"] == ["headed", "gpu"]
    assert "needs" not in run(["task", "add", "h9", "Browse"])


def test_task_set_needs_takes_a_comma_list():
    assert run(["task", "set", "h9", "needs=headed"])["fields"] == {"needs": ["headed"]}
    assert run(["task", "set", "h9", "needs="])["fields"] == {"needs": []}
