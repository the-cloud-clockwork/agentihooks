import pytest

from scripts.gates import intent
from scripts.swarm_ledger import plan_read
from tests.gates.test_intent import PR, chunk_classifier
from tests.swarm_ledger.test_plan_kind import add
from tests.swarm_ledger.test_plan_ranges import plan_ledger, published  # noqa: F401

pytestmark = pytest.mark.unit

TABLE = (
    "# Plan\n"
    "## Build\n"
    "<!-- slice: table -->\n"
    "### Tasks\n"
    "| Task | Work |\n"
    "| --- | --- |\n"
    "| t1 | Cache the reads |\n"
    "| t2 | Batch the writes after t1 |\n"
    "| Drop the old index | Remove it after the batch lands |\n"
    "## Ship\n"
    "Other\n"
)
SHARED = (
    "plan slice first already belongs to open task a: give each task its own slice anchor, "
    "a <!-- slice: name --> line above its own row or section, or group the tasks into one grouped task"
)


def row(task_id, title, **fields):
    return {
        "id": task_id,
        "title": title,
        "description": f"{title} as the plan row says.",
        "phase": "p1",
        "state": "open",
        "plan_slice": "table",
        "plan_lines": "3-9",
        **fields,
    }


@pytest.fixture
def table(tmp_path, monkeypatch):
    from scripts.swarm_ledger import HERE

    monkeypatch.syspath_prepend(str(HERE))
    from scripts.swarm_ledger import ledger_artifacts

    monkeypatch.setattr(ledger_artifacts.core, "LEDGER_DIR", tmp_path)
    file = ledger_artifacts.store("tables", "plan.md", TABLE.encode())
    ref = {"artifact": f"http://127.0.0.1:8765/artifacts/tables/{file['id']}", "lines": "2-9"}
    phase = {"id": "p1", "title": "Build", "description": "Fast integration.", "plan_ref": ref}
    tasks = [row("t1", "Cache the reads"), row("t2", "Batch the writes"), row("t3", "Drop the old index")]
    return {
        "overview": "Project intent",
        "artifacts": [{"plan": True, "file": file}],
        "phases": [phase],
        "tasks": tasks,
    }


def task(doc, task_id):
    return next(t for t in doc["tasks"] if t["id"] == task_id)


class TestRefusal:
    def test_adding_a_task_on_a_slice_another_open_task_holds_is_refused(self, published):  # noqa: F811
        assert add(published, "a", plan_slice="first")[1] == []
        state, rejected = add(published, "b", plan_slice="first")
        assert (rejected, state["_meta"]["warnings"]) == (["add-b"], [SHARED])
        assert not any(t["id"] == "b" for t in state["tasks"])

    def test_setting_a_task_onto_a_slice_another_open_task_holds_is_refused(self, published):  # noqa: F811
        from scripts.swarm_ledger import ledger_core as core

        add(published, "a", plan_slice="first")
        assert add(published, "b", plan_slice="second")[1] == []
        op = {"op": "task_update", "id": "move", "by": "planner", "item": "tasks/b", "fields": {"plan_slice": "first"}}
        core.check_op(op)
        state, rejected = core.sync(published, ops=[op])
        assert (rejected, state["_meta"]["warnings"]) == (["move"], [SHARED])
        assert (task(state, "b")["plan_slice"], task(state, "b")["plan_lines"]) == ("second", "7-9")

    def test_sharers_skip_done_tasks_and_members_of_the_same_group(self, table):
        table["tasks"][0]["group_members"] = ["t2"]
        table["tasks"][1]["merged_into"] = "t1"
        assert plan_read.sharers(table, task(table, "t1")) == ["t3"]
        assert plan_read.sharers(table, task(table, "t2")) == ["t3"]
        task(table, "t3")["state"] = "done"
        assert plan_read.sharers(table, task(table, "t1")) == []

    def test_resending_a_task_its_own_shared_slice_is_not_refused(self, table):
        from types import SimpleNamespace

        from scripts.swarm_ledger import ledger_tasks

        ctx = SimpleNamespace(refused=[])
        op = {"item": "tasks/t1", "fields": {"plan_slice": "table"}}
        assert ledger_tasks._set_slice(table, op, ctx) is True
        assert ctx.refused == []

    def test_a_row_naming_another_task_after_its_first_cell_is_not_its_scope(self, table):
        assert plan_read.own_rows("| t2 | Batch the writes after t1 |\n", "8-8", task(table, "t1")) == []
        prose = "| Notes | Cache the reads before the batch |\n"
        assert plan_read.own_rows(prose, "8-8", task(table, "t1")) == []

    def test_a_task_in_another_phase_or_on_other_lines_shares_nothing(self, table):
        task(table, "t2")["phase"] = "p2"
        task(table, "t3")["plan_lines"] = "9-9"
        assert plan_read.sharers(table, task(table, "t1")) == []


class TestIntent:
    def test_a_shared_table_slice_is_judged_only_on_the_row_naming_the_task(self, table):
        state = intent.state_of(table, task(table, "t2"), PR)
        assert (state["plan_lines"], state["plan_chunk"]) == ("8-8", "| t2 | Batch the writes after t1 |\n")
        assert intent.judge(state, decide=chunk_classifier()) == (
            "pass",
            "the phase can use it as delivered at probability 0.90, judged against plan lines 8-8; "
            "plan lines 3-9 are shared with tasks t1, t3; this task's scope is plan lines 8-8, the rows that name it",
        )

    def test_a_row_naming_the_task_title_is_its_scope(self, table):
        state = intent.state_of(table, task(table, "t3"), PR)
        assert (state["plan_lines"], state["plan_chunk"]) == (
            "9-9",
            "| Drop the old index | Remove it after the batch lands |\n",
        )

    def test_a_sharer_no_row_names_is_judged_on_its_task_text_and_says_so(self, table):
        table["tasks"].append(row("t4", "Rename the module"))
        state = intent.state_of(table, task(table, "t4"), PR)
        assert "plan_lines" not in state and "plan_chunk" not in state
        verdict, reason = intent.judge(state, decide=chunk_classifier())
        assert verdict == "pass"
        assert reason.endswith(
            "; plan lines 3-9 are shared with tasks t1, t2, t3, and no row names this task, so none of them is its scope"
        )

    def test_a_slice_one_open_task_holds_is_judged_whole(self, table):
        table["tasks"] = [table["tasks"][0]]
        state = intent.state_of(table, task(table, "t1"), PR)
        assert state["plan_lines"] == "3-9" and "shared_slice" not in state


class TestPlanRead:
    def test_plan_read_prints_the_task_row_as_scope_and_the_shared_slice_as_context(self, table):
        assert plan_read.read(table, "tables", "t1", None) == (
            "Scope: plan lines 3-9 are shared with tasks t2, t3; "
            "this task's scope is plan lines 7-7, the rows that name it.\n"
            "| t1 | Cache the reads |\n"
            "\nContext only, not scope:\n" + TABLE.replace("| t1 | Cache the reads |\n", "")
        )

    def test_plan_read_of_a_sharer_no_row_names_prints_its_description_as_scope(self, table):
        table["tasks"].append(row("t4", "Rename the module"))
        assert plan_read.read(table, "tables", "t4", None) == (
            "Scope: plan lines 3-9 are shared with tasks t1, t2, t3, and no row names this task, "
            "so none of them is its scope.\n"
            "Rename the module as the plan row says.\n"
            "\nContext only, not scope:\n" + TABLE
        )

    def test_plan_read_of_a_slice_one_open_task_holds_prints_the_chunk(self, table):
        table["tasks"] = [table["tasks"][0]]
        assert plan_read.read(table, "tables", "t1", None) == TABLE
