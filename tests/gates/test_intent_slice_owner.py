import json
import subprocess

import pytest

from scripts.gates import intent, lift
from scripts.gates.verdicts import Verdicts
from tests.gates.test_intent import PR, chunk_classifier
from tests.gates.test_intent import SLUG as COACHED
from tests.gates.test_intent import TASK as COACHED_TASK
from tests.gates.test_intent_coach import run_check

pytestmark = pytest.mark.unit

SLUG = "slices"
URL = "https://github.com/o/r/pull/9"
PLAN = (
    "# Plan\n"
    "<!-- slice: tasks -->\n"
    "## Tasks\n"
    "| t1 | Build the queue |\n"
    "| t2 | Ship the release |\n"
    "<!-- slice: own -->\n"
    "### Own work\n"
    "Do the own work.\n"
    "<!-- slice: t9 -->\n"
    "### t9 work\n"
    "Do the t9 work.\n"
)


@pytest.fixture
def doc(tmp_path, monkeypatch):
    from scripts.swarm_ledger import HERE

    monkeypatch.syspath_prepend(str(HERE))
    from scripts.swarm_ledger import ledger_artifacts

    monkeypatch.setattr(ledger_artifacts.core, "LEDGER_DIR", tmp_path)
    file = ledger_artifacts.store(SLUG, "plan.md", PLAN.encode())
    ref = {"artifact": f"http://127.0.0.1:8765/artifacts/{SLUG}/{file['id']}", "lines": "1-11"}
    return {
        "overview": "Swarm tooling.",
        "artifacts": [{"plan": True, "file": file}],
        "phases": [{"id": "p1", "title": "Runtime", "description": "Defects.", "plan_ref": ref}],
        "tasks": [],
    }


def task(task_id, plan_slice, lines, phase="p1"):
    return {
        "id": task_id,
        "phase": phase,
        "title": f"Task {task_id}",
        "description": f"The description of {task_id}.",
        "plan_slice": plan_slice,
        "plan_lines": lines,
    }


def state(doc, *tasks):
    full = {**doc, "tasks": list(tasks)}
    return intent.state_of(full, tasks[0], PR)


def test_a_task_alone_on_its_own_slice_is_graded_against_its_chunk(doc):
    graded = state(doc, task("t5", "own", "6-8"), task("t1", "tasks", "2-5"))
    assert (graded["plan_lines"], graded["plan_chunk"]) == (
        "6-8",
        "<!-- slice: own -->\n### Own work\nDo the own work.\n",
    )


def test_tasks_sharing_a_table_slice_are_graded_against_their_own_rows(doc):
    first, second = task("t1", "tasks", "2-5"), task("t2", "tasks", "2-5")
    graded = state(doc, first, second)
    assert (graded["plan_lines"], graded["plan_chunk"]) == ("4-4", "| t1 | Build the queue |\n")
    assert state(doc, second, first)["plan_chunk"] == "| t2 | Ship the release |\n"


def test_tasks_sharing_a_slice_that_names_neither_are_graded_against_their_own_description(doc):
    first, second = task("t3", "tasks", "2-5"), task("t4", "tasks", "2-5")
    for graded in (state(doc, first, second), state(doc, second, first)):
        assert "plan_chunk" not in graded and "plan_lines" not in graded
    assert state(doc, first, second)["task_text"] == "The description of t3."


def test_a_slice_named_after_another_task_belongs_to_that_task(doc):
    owner, borrower = task("t9", "t9", "9-11"), task("t4", "t9", "9-11")
    assert "plan_chunk" not in state(doc, borrower, owner)
    assert state(doc, owner, borrower)["plan_chunk"] == "<!-- slice: t9 -->\n### t9 work\nDo the t9 work.\n"


def test_a_slice_named_after_a_task_on_another_slice_still_belongs_to_that_task(doc):
    assert "plan_chunk" not in state(doc, task("t4", "t9", "9-11"), task("t9", "own", "6-8"))


def test_a_document_without_tasks_leaves_a_marked_slice_to_its_task():
    assert intent._foreign_slice({}, task("t4", "own", "6-8"), "<!-- slice: own -->\n") is False


def test_the_same_slice_name_in_another_phase_is_not_shared(doc):
    graded = state(doc, task("t5", "own", "6-8"), task("t6", "own", "6-8", phase="p2"))
    assert graded["plan_lines"] == "6-8"


def test_stored_lines_that_no_longer_hold_the_tasks_slice_are_ignored(doc):
    assert "plan_chunk" not in state(doc, task("t5", "own", "2-5"))


def test_a_slice_name_only_in_prose_does_not_mark_the_lines_as_the_tasks_own(doc):
    assert "plan_chunk" not in state(doc, task("t5", "own", "8-8"))
    assert state(doc, task("t9", "t9", "10-11"))["plan_lines"] == "10-11"


def test_lines_without_a_slice_name_keep_their_chunk(doc):
    assert state(doc, task("t5", "", "6-8"))["plan_lines"] == "6-8"


def test_an_intent_verdict_on_a_placeholder_slice_is_graded_from_the_description(doc):
    asked = []
    graded = state(doc, task("t3", "tasks", "2-5"), task("t4", "tasks", "2-5"))
    verdict, reason = intent.judge(graded, decide=chunk_classifier(underdelivers=0.99, seen=asked))
    assert (verdict, reason.split(";")[0]) == ("pass", "the phase can use it as delivered at probability 0.90")
    assert reason.endswith(
        "plan lines 2-5 are shared with tasks t4, and no row names this task, so none of them is its scope"
    )
    assert asked == [list(intent.BASE_QUESTIONS)]


class Ran:
    def __init__(self, returncode, stdout):
        self.returncode, self.stdout, self.calls = returncode, stdout, []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "")


@pytest.mark.parametrize(
    "returncode, stdout, unchanged",
    [
        (0, json.dumps(["ahead", ["tests/gates/test_a.py", "docs/a.md"]]), True),
        (0, json.dumps(["ahead", []]), True),
        (0, json.dumps(["ahead", ["tests/a.py", "scripts/gates/intent.py"]]), False),
        (0, json.dumps(["diverged", ["tests/a.py"]]), False),
        (0, json.dumps(["ahead", [f"tests/test_{n}.py" for n in range(intent.COMPARE_FILES)]]), False),
        (0, json.dumps(["ahead", [f"tests/test_{n}.py" for n in range(intent.COMPARE_FILES - 1)]]), True),
        (1, "", False),
        (0, "not json", False),
    ],
)
def test_production_is_unchanged_only_when_the_new_head_adds_tests_or_docs(returncode, stdout, unchanged):
    ran = Ran(returncode, stdout)
    assert intent.production_unchanged(URL, "old", "new", run=ran) is unchanged
    assert ran.calls == [["gh", "api", "repos/o/r/compare/old...new", "--jq", intent.COMPARED]]


def test_production_is_never_unchanged_for_a_link_that_is_not_a_pull_request():
    ran = Ran(0, json.dumps(["ahead", []]))
    assert intent.production_unchanged("https://example.com/x", "old", "new", run=ran) is False
    assert ran.calls == []


def lifted_on(tmp_path, monkeypatch, head, unchanged):
    path = lift.task_lift_path(SLUG, "t1", "intent", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": 0, "url": URL, "head": "old"}))
    compared = []
    monkeypatch.setattr(intent, "pr_head", lambda url: head)
    monkeypatch.setattr(
        intent, "production_unchanged", lambda url, before, after: compared.append((url, before, after)) or unchanged
    )
    return lift.task_lifted(SLUG, "t1", "intent", tmp_path), compared


def test_a_master_lift_carries_to_a_new_head_that_changes_no_production_file(tmp_path, monkeypatch):
    assert lifted_on(tmp_path, monkeypatch, "new", True) == (True, [(URL, "old", "new")])


def test_a_master_lift_ends_on_a_new_head_that_changes_production(tmp_path, monkeypatch):
    assert lifted_on(tmp_path, monkeypatch, "new", False) == (False, [(URL, "old", "new")])


def test_a_master_lift_holds_on_its_own_head_without_comparing(tmp_path, monkeypatch):
    assert lifted_on(tmp_path, monkeypatch, "old", False) == (True, [])


def test_a_master_lift_ends_when_the_head_cannot_be_read(tmp_path, monkeypatch):
    assert lifted_on(tmp_path, monkeypatch, None, True) == (False, [])


def test_a_master_lift_without_a_recorded_head_never_holds(tmp_path, monkeypatch):
    path = lift.task_lift_path(SLUG, "t1", "intent", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": 0, "url": URL}))
    monkeypatch.setattr(intent, "pr_head", lambda url: None)
    assert lift.task_lifted(SLUG, "t1", "intent", tmp_path) is False


def lift_coached(tmp_path, monkeypatch, unchanged, record=None):
    run_check(tmp_path, "first")
    path = lift.task_lift_path(COACHED, COACHED_TASK, intent.NAME, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record or {"at": 0, "url": URL, "head": "first"}))
    monkeypatch.setattr(intent, "pr_head", lambda url: "second")
    monkeypatch.setattr(intent, "production_unchanged", lambda url, before, after: unchanged)
    return run_check(tmp_path, "second"), Verdicts(COACHED, intent.NAME, tmp_path).read(COACHED_TASK)


def test_the_tick_keeps_a_lifted_verdict_on_a_head_that_adds_only_tests(tmp_path, monkeypatch):
    (ledger, mail), record = lift_coached(tmp_path, monkeypatch, True)
    assert (ledger.updates, ledger.comments, mail.sent) == ([], [], [])
    assert (record["head"], record["coach_rounds"]) == ("first", 0)


def test_the_tick_judges_again_a_lifted_task_whose_new_head_changes_production(tmp_path, monkeypatch):
    (ledger, mail), record = lift_coached(tmp_path, monkeypatch, False)
    assert ledger.updates == [(COACHED, COACHED_TASK, {"state": "claimed"}, "swarm")]
    assert "fix round 2 of 2" in mail.sent[0][2]
    assert record["head"] == "second"


def test_the_tick_judges_again_a_task_whose_lift_is_bound_to_no_pull_request(tmp_path, monkeypatch):
    import time

    (ledger, _), record = lift_coached(tmp_path, monkeypatch, True, record={"at": time.time()})
    assert ledger.updates == [(COACHED, COACHED_TASK, {"state": "claimed"}, "swarm")]
    assert record["head"] == "second"
