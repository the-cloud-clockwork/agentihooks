import argparse
import json
import sys
import unittest.mock
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402

from scripts.swarm_ledger import ledger, ledger_kinds, workflow_task  # noqa: E402
from tests.swarm.test_cli import env, run  # noqa: E402, F401
from tests.swarm_ledger import test_task_kinds as kinds  # noqa: E402

NAME = "engineer@a1b2c3-0001"
WORKFLOW = {"task": "t1", "title": "run the loop", "lane": "eng", "kind": "workflow", "workflow": "simple-workflow"}


def add(*extra, case=None):
    sent = []
    argv = ["--slug", kinds.SLUG, "--as", "liaison", "task", "add", "t5", "run", "the", "loop", "--kind", "workflow"]
    argv += [*extra, *(["--case", str(case)] if case else [])]
    with unittest.mock.patch.object(ledger, "send", lambda args, kind, /, **f: sent.append((kind, f))):
        ledger.cmd_task(ledger.build_parser().parse_args(argv))
    return sent


def test_task_add_takes_the_workflow_kind_with_its_name_and_case(tmp_path):
    case = tmp_path / "case.md"
    case.write_text("the case")
    sent = add("--workflow", "simple-workflow", case=case)
    fields = sent[0][1]
    assert (fields["kind"], fields["workflow"], fields["case"]) == ("workflow", "simple-workflow", str(case))
    assert (fields["lane"], fields["profile"]) == ("eng", "engineer")


def test_the_ledger_stores_a_workflow_task_with_its_name_and_case():
    task = {**WORKFLOW, "case": "/cases/one.md"}
    core.check_op(kinds.op("task_add", 1, **task))
    state = kinds.make_ledger([task])
    assert (state["tasks"][0]["kind"], state["tasks"][0]["workflow"], state["tasks"][0]["case"]) == (
        "workflow",
        "simple-workflow",
        "/cases/one.md",
    )


@pytest.mark.parametrize(
    "extra",
    [("--workflow", "no-such-workflow"), (), ("--workflow", "simple-workflow", "--lane", "ci")],
)
def test_task_add_refuses_an_unknown_missing_or_misplaced_workflow(extra):
    with pytest.raises(SystemExit):
        add(*extra)


def test_the_ledger_refuses_a_workflow_field_on_another_kind_or_a_workflow_task_without_one():
    for bad in (
        kinds.op("task_add", 1, task="t1", title="a", lane="eng", workflow="simple-workflow"),
        kinds.op("task_add", 2, task="t1", title="a", lane="eng", kind="workflow"),
        kinds.op("task_add", 3, **{**WORKFLOW, "lane": "ci"}),
    ):
        with pytest.raises(ValueError):
            core.check_op(bad)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"kind": "workflow", "workflow": 3}, "workflow and case must be strings"),
        (
            {"kind": "workflow", "workflow": " "},
            "a workflow task names its workflow, and only a workflow task takes one",
        ),
        (
            {"kind": "ops", "workflow": "simple-workflow"},
            "a workflow task names its workflow, and only a workflow task takes one",
        ),
        ({**WORKFLOW, "lane": "ci"}, "workflow tasks use the eng lane"),
    ],
)
def test_check_workflow_names_what_it_refuses(fields, message):
    with pytest.raises(ValueError) as refused:
        ledger_kinds.check_workflow(fields)
    assert str(refused.value) == message


@pytest.mark.parametrize(
    "fields",
    [WORKFLOW, {"kind": "workflow", "workflow": "simple-workflow"}, {"kind": "ops", "lane": "ci"}, {}],
)
def test_check_workflow_takes_a_named_workflow_in_the_eng_lane_or_by_default_and_other_kinds_without_one(fields):
    assert ledger_kinds.check_workflow(fields) is None


def test_task_add_documents_the_workflow_kind_and_its_options():
    parser = ledger.build_parser()
    parser = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices["task"]
    helps = {action.dest: action.help for action in parser._actions}
    assert helps["kind"] == "code (default), ci, ops, troubleshoot, tune, research, workflow"
    assert helps["workflow"] == "workflow kind: the workflow name"
    assert helps["case"] == "workflow kind: the case file"


def test_the_ledger_refuses_done_without_a_passed_workflow_and_a_pass_verdict():
    kinds.make_ledger([WORKFLOW])
    for n, proof in enumerate(({}, {"workflow": "exhausted", "verdict": "pass"}, {"workflow": "passed"}), 1):
        state, rejected = core.sync(kinds.SLUG, ops=[kinds.done(n, **proof)])
        assert rejected and state["tasks"][0]["state"] != "done"
    assert ledger_kinds.unmet({"kind": "workflow", "proof": {"workflow": "passed", "verdict": "fail"}}) == ["verdict"]
    state, rejected = core.sync(kinds.SLUG, ops=[kinds.done(9, workflow="passed", verdict="pass")])
    assert (rejected, state["tasks"][0]["state"]) == ([], "done")


def _workflow_task(env, monkeypatch, tmp_path, state=None, verdict=None):  # noqa: F811
    _, rows, _ = env
    if state is not None:
        (tmp_path / workflow_task.STATE).write_text(json.dumps(state))
    if verdict is not None:
        (tmp_path / workflow_task.VERDICT).write_text(json.dumps(verdict))
    rows.rows["t1"].update(kind="workflow", workflow="simple-workflow")
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    rows.rows["t1"]["workspace"] = str(tmp_path)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", NAME)
    return rows


def test_swarm_done_refuses_a_workflow_task_without_a_passed_workflow_state(env, monkeypatch, tmp_path, capsys):  # noqa: F811
    rows = _workflow_task(env, monkeypatch, tmp_path, verdict={"verdict": "pass"})
    assert run("sw", "done") == 1
    assert "workflow" in capsys.readouterr().err
    assert rows.rows["t1"]["state"] != "done"


def test_swarm_done_refuses_a_workflow_task_whose_verdict_is_not_pass(env, monkeypatch, tmp_path):  # noqa: F811
    rows = _workflow_task(env, monkeypatch, tmp_path, {"status": "passed"}, {"verdict": "fail", "notes": "n"})
    assert run("sw", "done", "--workflow", "passed", "--verdict", "pass") == 1
    assert rows.rows["t1"]["state"] != "done"


def test_swarm_done_closes_a_workflow_task_with_its_passed_state_and_verdict(env, monkeypatch, tmp_path):  # noqa: F811
    rows = _workflow_task(env, monkeypatch, tmp_path, {"status": "passed"}, {"verdict": "pass"})
    assert run("sw", "done") == 0
    assert rows.rows["t1"]["state"] == "done"
    assert rows.rows["t1"]["proof"] == {"workflow": "passed", "verdict": "pass"}


def test_an_exhausted_workflow_blocks_its_task_with_the_last_judge_notes(env, monkeypatch, tmp_path, capsys):  # noqa: F811
    notes = "the build never handles the retry path"
    rows = _workflow_task(env, monkeypatch, tmp_path, {"status": "exhausted", "last_notes": notes})
    assert run("sw", "done") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["state"] == "blocked"
    assert rows.rows["t1"]["state"] == "blocked"
    assert rows.comments[-1] == ("t1", notes, NAME)


def test_the_workspace_files_are_workflow_json_and_verdict_json():
    assert (workflow_task.STATE, workflow_task.VERDICT) == ("workflow.json", "verdict.json")


def test_add_fields_keeps_a_named_profile_and_omits_an_absent_case():
    assert workflow_task.add_fields("simple-workflow", "", "eng", "reviewer", Path.cwd()) == {
        "workflow": "simple-workflow",
        "profile": "reviewer",
    }


@pytest.mark.parametrize(
    ("name", "case", "lane", "message"),
    [
        ("no-such-workflow", "", "eng", "unknown workflow no-such-workflow"),
        ("simple-workflow", "", "ci", "workflow tasks use the eng lane"),
        ("simple-workflow", "missing-case.md", "eng", "case not found: missing-case.md"),
    ],
)
def test_add_fields_names_what_it_refuses(name, case, lane, message):
    with pytest.raises(ValueError) as refused:
        workflow_task.add_fields(name, case, lane, None, Path.cwd())
    assert str(refused.value) == message


@pytest.mark.parametrize("text", [None, "not json", "[1, 2]"])
def test_read_gives_an_empty_record_for_a_missing_broken_or_non_object_file(tmp_path, text):
    if text is not None:
        (tmp_path / workflow_task.STATE).write_text(text)
    assert workflow_task.read(str(tmp_path), workflow_task.STATE) == {}
    assert workflow_task.read("", workflow_task.STATE) == {}


def test_proof_and_notes_of_an_empty_workspace(tmp_path):
    assert workflow_task.proof(str(tmp_path)) == {"workflow": "", "verdict": ""}
    assert workflow_task.exhausted_notes(str(tmp_path)) is None


def test_exhausted_notes_fall_back_when_the_judge_left_none(tmp_path):
    (tmp_path / workflow_task.STATE).write_text(json.dumps({"status": "passed", "last_notes": "kept"}))
    assert workflow_task.exhausted_notes(str(tmp_path)) is None
    (tmp_path / workflow_task.STATE).write_text(json.dumps({"status": "exhausted", "last_notes": ""}))
    assert workflow_task.exhausted_notes(str(tmp_path)) == "workflow exhausted"
