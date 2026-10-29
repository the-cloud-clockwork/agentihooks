import ledger_core as core
import pytest

from scripts.swarm import prompt
from scripts.swarm_ledger import ledger, ledger_tasks
from tests.swarm_ledger import test_artifact_lifecycle as lifecycle
from tests.swarm_ledger.test_artifact_lifecycle import AGENT, add_task, storage
from tests.swarm_ledger.test_artifact_lifecycle import package_modules as package_modules
from tests.swarm_ledger.test_artifact_lifecycle import slug as slug


def test_a_task_added_or_set_for_the_workstation_keeps_the_flag(slug):
    state, _ = add_task(slug, "w1", workstation=True)
    assert next(t for t in state["tasks"] if t["id"] == "w1")["workstation"] is True
    update = {"op": "task_update", "id": "u", "by": AGENT, "item": "tasks/w1", "fields": {"workstation": False}}
    state, _ = storage.apply_ops(slug, ops=[update])
    assert next(t for t in state["tasks"] if t["id"] == "w1")["workstation"] is False


def test_the_workstation_flag_must_be_a_boolean():
    assert "workstation" in ledger_tasks.UPDATABLE
    task = {"op": "task_add", "id": "t", "by": "eng", "task": "w", "title": "Work", "lane": "eng"}
    update = {"op": "task_update", "id": "u", "by": "eng", "item": "tasks/w", "fields": {"workstation": "yes"}}
    for bad in ({**task, "workstation": "yes"}, update):
        with pytest.raises(ValueError, match="^workstation must be true or false$"):
            core.check_op(bad)


def test_the_master_marks_tasks_that_act_on_the_workstation():
    text = prompt.build("demo", "/repo", "master", "agent", {"id": "one", "title": "One"})
    assert f"- {prompt.WORKSTATION_LINE}" in text
    assert "--workstation" in prompt.WORKSTATION_LINE and "workstation=yes" in prompt.WORKSTATION_LINE


def run(argv):
    return lifecycle.TestCommands().run(argv)


def options(parser):
    for action in parser._actions:
        yield action
        if isinstance(action.choices, dict):
            for child in action.choices.values():
                yield from options(child)


def test_task_add_help_names_the_workstation_flag():
    flag = next(action for action in options(ledger.build_parser()) if "--workstation" in action.option_strings)
    assert flag.help == "the task acts on the workstation checkout, so no remote hive runs it"
    assert flag.default is False


def test_task_add_workstation_marks_the_task():
    assert run(["task", "add", "w9", "Landing", "--workstation"])["workstation"] is True
    assert "workstation" not in run(["task", "add", "w9", "Landing"])


def test_task_set_workstation_yes_and_no():
    assert run(["task", "set", "w9", "workstation=yes"])["fields"] == {"workstation": True}
    assert run(["task", "set", "w9", "workstation=no"])["fields"] == {"workstation": False}
    with pytest.raises(SystemExit) as exc:
        run(["task", "set", "w9", "workstation=maybe"])
    assert exc.value.code == "task set takes workstation=yes or workstation=no"
