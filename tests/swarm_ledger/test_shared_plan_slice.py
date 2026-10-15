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
PADDED = (
    "# Plan\n"
    "## Intro\n" + "".join(f"Note {n}\n" for n in range(1, 11)) + "## Build\n"
    "<!-- slice: table -->\n"
    "### Tasks\n"
    "| Task | Work |\n"
    "| --- | --- |\n"
    "| t1 | Cache the reads, see [ship](#ship) |\n"
    "| t1 | Warm the cache |\n"
    "| t2 | Batch the writes after t1 |\n"
    "| t1 | Evict late |\n"
    "## Notes\n"
    "Keep it\n"
    '<a id="ship"></a>\n'
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


def stored(tmp_path, monkeypatch, text, tasks):
    from scripts.swarm_ledger import HERE

    monkeypatch.syspath_prepend(str(HERE))
    from scripts.swarm_ledger import ledger_artifacts

    monkeypatch.setattr(ledger_artifacts.core, "LEDGER_DIR", tmp_path)
    file = ledger_artifacts.store("tables", "plan.md", text.encode())
    ref = {"artifact": f"http://127.0.0.1:8765/artifacts/tables/{file['id']}", "lines": "2-9"}
    phase = {"id": "p1", "title": "Build", "description": "Fast integration.", "plan_ref": ref}
    return {
        "overview": "Project intent",
        "artifacts": [{"plan": True, "file": file}],
        "phases": [phase],
        "tasks": tasks,
    }


@pytest.fixture
def table(tmp_path, monkeypatch):
    tasks = [row("t1", "Cache the reads"), row("t2", "Batch the writes"), row("t3", "Drop the old index")]
    return stored(tmp_path, monkeypatch, TABLE, tasks)


@pytest.fixture
def padded(tmp_path, monkeypatch):
    tasks = [row("t1", "Cache the reads", plan_lines="14-21"), row("t2", "Batch the writes", plan_lines="14-21")]
    return stored(tmp_path, monkeypatch, PADDED, tasks)


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

    def test_the_refusal_names_every_open_task_holding_the_slice(self, table):
        from scripts.swarm_ledger import ledger_tasks

        assert ledger_tasks.shared_refusal(table, row("t4", "Rename the module")).startswith(
            "plan slice table already belongs to open task t1, t2, t3: give each task"
        )

    def test_a_ledger_without_tasks_has_no_sharers(self, table):
        assert plan_read.sharers({}, task(table, "t1")) == []

    def test_scope_is_the_first_run_of_rows_naming_the_task(self, padded):
        rows = [(18, "| t1 | Cache the reads, see [ship](#ship) |"), (19, "| t1 | Warm the cache |")]
        assert (
            plan_read.own_rows("".join(f"{r}\n" for r in PADDED.splitlines()[13:21]), "14-21", task(padded, "t1"))
            == rows
        )

    def test_a_listed_row_or_an_untitled_task_is_matched_by_its_id(self):
        assert plan_read.own_rows("- t1 cache the reads\n", "5-5", {"id": "t1"}) == [(5, "- t1 cache the reads")]
        assert plan_read.own_rows("| xxxx | Cache the reads |\n", "5-5", {"id": "t1"}) == []

    def test_a_line_outside_a_table_is_matched_by_its_whole_title(self):
        assert plan_read.own_rows("Drop index X\n", "5-5", {"id": "t9", "title": "Drop index X"}) == [
            (5, "Drop index X")
        ]

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

    def test_plan_read_context_keeps_the_margin_and_skips_linked_sections_already_printed(self, padded):
        context = (
            "".join(f"Note {n}\n" for n in range(2, 11))
            + "## Build\n<!-- slice: table -->\n### Tasks\n| Task | Work |\n| --- | --- |\n"
            "| t2 | Batch the writes after t1 |\n| t1 | Evict late |\n## Notes\nKeep it\n"
            '<a id="ship"></a>\n## Ship\nOther\n'
        )
        assert plan_read.read(padded, "tables", "t1", None) == (
            "Scope: plan lines 14-21 are shared with tasks t2; "
            "this task's scope is plan lines 18-19, the rows that name it.\n"
            "| t1 | Cache the reads, see [ship](#ship) |\n| t1 | Warm the cache |\n"
            "\nContext only, not scope:\n" + context
        )

    def test_plan_read_context_ends_ten_lines_after_the_slice(self, tmp_path, monkeypatch):
        text = PADDED + "## End\n" + "".join(f"Tail {n}\n" for n in range(1, 9))
        tasks = [row("t1", "Cache the reads", plan_lines="14-21"), row("t2", "Batch the writes", plan_lines="14-21")]
        doc = stored(tmp_path, monkeypatch, text, tasks)
        assert plan_read.read(doc, "tables", "t1", None).endswith("## End\nTail 1\nTail 2\nTail 3\nTail 4\n")

    def test_plan_read_of_an_undescribed_sharer_no_row_names_prints_an_empty_scope(self, table):
        table["tasks"].append(row("t4", "Rename the module"))
        del table["tasks"][-1]["description"]
        assert plan_read.read(table, "tables", "t4", None) == (
            "Scope: plan lines 3-9 are shared with tasks t1, t2, t3, and no row names this task, "
            "so none of them is its scope.\n\n\nContext only, not scope:\n" + TABLE
        )

    def test_plan_read_of_a_slice_one_open_task_holds_prints_the_chunk(self, table):
        table["tasks"] = [table["tasks"][0]]
        assert plan_read.read(table, "tables", "t1", None) == TABLE
