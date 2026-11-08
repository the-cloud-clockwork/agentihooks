"""Claimed workflows advance through the runner without advancing pending seat work."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from scripts.workflows import machine, schema, store
from scripts.workflows.runner import StageResult

pytestmark = pytest.mark.feature("swarm-workflow-runner")


def definition() -> schema.Workflow:
    return schema.parse(
        yaml.safe_dump(
            {
                "workflow": {
                    "name": "bridge",
                    "intent": "Prove tick advancement",
                    "head": "build",
                    "max_iterations": 2,
                    schema.EXIT: "review.pass",
                    "on_exhausted": "block",
                },
                "stages": {
                    "build": {"profile": "engineer", "input": ["case"], "next": {"pass": "review"}},
                    "review": {"profile": "judge", "input": ["build"], "next": {"pass": schema.EXIT, "retry": "build"}},
                },
            }
        )
    )


def task(folder: Path) -> dict:
    return {
        "id": "work",
        "kind": "workflow",
        "workflow": "bridge",
        "state": "claimed",
        "claimed_by": "engineer@123456-1234",
        "workspace": str(folder),
        "description": "Build the case",
        "claim_generation": 1,
    }


def resolve(monkeypatch: pytest.MonkeyPatch) -> schema.Workflow:
    from scripts.swarm import workflow_runner

    flow = definition()
    monkeypatch.setattr(
        workflow_runner.loader, "resolve", lambda repo: ({"bridge": SimpleNamespace(workflow=flow)}, [])
    )
    return flow


@pytest.mark.feature("swarm-workflow-runner")
def test_pending_head_is_delivered_once_without_consuming_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import workflow_runner

    resolve(monkeypatch)
    inbox = Mock()
    row = task(tmp_path)
    doc = {"tasks": [row]}
    assert workflow_runner.advance_tasks(inbox, doc, tmp_path) == ["workflow work waits for its claimant"]
    before = store.load(tmp_path)
    assert (before["stage"], before["status"], before["visit"], before["runs"]) == ("build", "running", 0, {})
    assert before["case"] == "Build the case"
    assert before["definition"] == definition().to_document()
    assert before["harness"] == "auto"
    assert before["workflow"] == "bridge"
    assert before["run"] == tmp_path.name
    assert (tmp_path / "outputs" / "build" / "seat-1-0-0.sent").read_text() == row["claimed_by"]
    assert workflow_runner.advance_tasks(inbox, doc, tmp_path) == ["workflow work waits for its claimant"]
    assert store.load(tmp_path) == before
    assert inbox.send.call_count == 1
    args, kwargs = inbox.send.call_args
    assert args[:2] == ("swarm", row["claimed_by"])
    assert kwargs["task"] == "work"
    assert '"case": "Build the case"' in args[2]
    assert args[2] == (
        "Work the build workflow stage for your claimed task work. "
        'Inputs: {"case": "Build the case"}\n'
        f"Write the result to {tmp_path / 'outputs' / 'build' / 'seat-1-0-0.json'} "
        "as JSON with outcome (pass, retry or enhance) and outputs. "
        "The tick resumes the workflow after that result exists."
    )


@pytest.mark.feature("swarm-workflow-runner")
def test_head_result_advances_once_then_other_stage_uses_headless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import workflow_runner

    resolve(monkeypatch)
    fallback = Mock()
    fallback.launch.return_value = StageResult("pass", {"verdict": "pass"})
    monkeypatch.setattr(workflow_runner, "HeadlessProfile", lambda **kwargs: fallback)
    monkeypatch.setattr(workflow_runner.exit_checks, "check", lambda expression, outputs: True)
    row = task(tmp_path)
    doc = {"tasks": [row]}
    inbox = Mock()
    workflow_runner.advance_tasks(inbox, doc, tmp_path)
    result = tmp_path / "outputs" / "build" / "seat-1-0-0.json"
    result.write_text(json.dumps({"outcome": "pass", "outputs": "built"}))
    workflow_runner.advance_tasks(inbox, doc, tmp_path)
    assert (store.load(tmp_path)["stage"], store.load(tmp_path)["outputs"]) == ("review", {"build": "built"})
    fallback.launch.assert_not_called()
    workflow_runner.advance_tasks(inbox, doc, tmp_path)
    fallback.launch.assert_called_once_with(
        definition().stages["review"], {"case": "Build the case", "build": "built"}, tmp_path
    )
    assert store.load(tmp_path)["status"] == "passed"
    assert workflow_runner.advance_tasks(inbox, doc, tmp_path) == []
    assert fallback.launch.call_count == 1


@pytest.mark.parametrize(
    "changes", [{"kind": "code"}, {"state": "open"}, {"state": "done"}, {"done": True}, {"claimed_by": ""}]
)
@pytest.mark.feature("swarm-workflow-runner")
def test_only_claimed_workflow_tasks_run(tmp_path: Path, changes: dict) -> None:
    from scripts.swarm import workflow_runner

    inbox = Mock()
    assert workflow_runner.advance_tasks(inbox, {"tasks": [{**task(tmp_path), **changes}]}, tmp_path) == []
    assert not (tmp_path / "workflow.json").exists()
    inbox.send.assert_not_called()


@pytest.mark.feature("swarm-workflow-runner")
def test_existing_state_and_case_survive_definition_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import workflow_runner

    flow = resolve(monkeypatch)
    case = tmp_path / "case.md"
    case.write_text("Original case")
    row = {**task(tmp_path), "case": str(case)}
    workflow_runner.advance_tasks(Mock(), {"tasks": [row]}, tmp_path)
    state = store.load(tmp_path)
    assert state["case"] == "Original case"
    case.write_text("Changed case")
    monkeypatch.setattr(
        workflow_runner.loader, "resolve", Mock(side_effect=AssertionError("must resume stored definition"))
    )
    workflow_runner.advance_tasks(Mock(), {"tasks": [row]}, tmp_path)
    assert store.load(tmp_path)["definition"] == flow.to_document()
    assert store.load(tmp_path)["case"] == "Original case"
    assert store.load(tmp_path) == state


@pytest.mark.feature("swarm-workflow-runner")
def test_bad_task_does_not_prevent_the_next_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import workflow_runner

    resolve(monkeypatch)
    bad = {**task(tmp_path / "bad"), "workflow": "missing", "id": "bad"}
    good = {**task(tmp_path / "good"), "id": "good"}
    actions = workflow_runner.advance_tasks(Mock(), {"tasks": [bad, good]}, tmp_path)
    assert any("bad" in action and "missing" in action for action in actions)
    assert store.load(tmp_path / "good")["status"] == machine.RUNNING


@pytest.mark.parametrize("stage_id", ["build", "review"])
@pytest.mark.feature("swarm-workflow-runner")
def test_filtered_stages_use_judge_including_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage_id: str
) -> None:
    from dataclasses import replace

    from scripts.swarm import workflow_runner
    from scripts.workflows import launchers

    flow = definition()
    flow.stages[stage_id] = replace(flow.stages[stage_id], filters=("judge-blind",))
    monkeypatch.setattr(
        workflow_runner.loader, "resolve", lambda repo: ({"bridge": SimpleNamespace(workflow=flow)}, [])
    )
    judge = Mock(return_value=StageResult("pass", {"verdict": "pass"}))
    monkeypatch.setattr(launchers.Judge, "launch", judge)
    row = task(tmp_path)
    workflow_runner.initialize(row, tmp_path, tmp_path)
    if stage_id == "review":
        state = store.load(tmp_path)
        state["stage"] = stage_id
        state["outputs"] = {"build": "built"}
        store.save(tmp_path, state)
    inbox = Mock()
    workflow_runner.advance_task(inbox, row, tmp_path)
    judge.assert_called_once_with(
        flow.stages[stage_id],
        {"case": "Build the case", **({"build": "built"} if stage_id == "review" else {})},
        tmp_path,
    )
    inbox.send.assert_not_called()


@pytest.mark.feature("swarm-workflow-runner")
def test_scouter_stage_uses_scout_after_claimant_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from scripts.swarm import workflow_runner
    from scripts.workflows import launchers

    flow = definition()
    flow.stages["review"] = replace(flow.stages["review"], profile="scouter")
    monkeypatch.setattr(
        workflow_runner.loader, "resolve", lambda repo: ({"bridge": SimpleNamespace(workflow=flow)}, [])
    )
    scout = Mock(return_value=StageResult("pass", [{"items": []}]))
    monkeypatch.setattr(launchers.Scout, "launch", scout)
    monkeypatch.setattr(workflow_runner.exit_checks, "check", lambda expression, outputs: True)
    inbox = Mock()
    row = task(tmp_path)
    workflow_runner.advance_task(inbox, row, tmp_path)
    result = tmp_path / "outputs" / "build" / "seat-1-0-0.json"
    result.write_text(json.dumps({"outcome": "pass", "outputs": "built"}))
    workflow_runner.advance_task(inbox, row, tmp_path)
    workflow_runner.advance_task(inbox, row, tmp_path)
    scout.assert_called_once_with(flow.stages["review"], {"case": "Build the case", "build": "built"}, tmp_path)
    assert inbox.send.call_count == 1
    assert store.load(tmp_path)["status"] == "passed"


@pytest.mark.feature("swarm-workflow-runner")
def test_new_nested_workspace_forwards_repository_and_records_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import workflow_runner

    flow = definition()
    loaded = Mock(return_value=({"bridge": SimpleNamespace(workflow=flow)}, []))
    monkeypatch.setattr(workflow_runner.loader, "resolve", loaded)
    folder = tmp_path / "missing" / "run"
    row = task(folder)
    del row["claim_generation"]
    inbox = Mock()
    assert workflow_runner.advance_tasks(inbox, {"tasks": [row]}, tmp_path) == ["workflow work waits for its claimant"]
    loaded.assert_called_once_with(tmp_path)
    assert store.load(folder)["workflow"] == "bridge"
    assert store.load(folder)["run"] == "run"
    assert (folder / "outputs" / "build" / "seat-0-0-0.sent").read_text() == row["claimed_by"]
    assert str(folder / "outputs" / "build" / "seat-0-0-0.json") in inbox.send.call_args.args[2]


@pytest.mark.feature("swarm-workflow-runner")
def test_definition_errors_preserve_every_message_and_empty_documents_are_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import workflow_runner

    monkeypatch.setattr(workflow_runner.loader, "resolve", Mock(return_value=({}, ["first error", "second error"])))
    assert workflow_runner.advance_tasks(Mock(), {}, tmp_path) == []
    assert workflow_runner.advance_tasks(Mock(), {"tasks": [task(tmp_path)]}, tmp_path) == [
        "workflow work refused: first error\nsecond error"
    ]


@pytest.mark.feature("swarm-workflow-runner")
def test_ignored_task_does_not_hide_a_later_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import workflow_runner

    resolve(monkeypatch)
    ignored = {**task(tmp_path / "ignored"), "kind": "code"}
    good = task(tmp_path / "good")
    assert workflow_runner.advance_tasks(Mock(), {"tasks": [ignored, good]}, tmp_path) == [
        "workflow work waits for its claimant"
    ]
    assert not (tmp_path / "ignored").exists()
    assert store.load(tmp_path / "good")["status"] == "running"


@pytest.mark.feature("swarm-workflow-runner")
def test_runner_receives_stored_harness_and_exit_checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import workflow_runner

    flow = resolve(monkeypatch)
    row = task(tmp_path)
    state = workflow_runner.initialize(row, tmp_path, tmp_path)
    state["harness"] = "codex"
    store.save(tmp_path, state)
    profile = Mock()
    judge = Mock()
    routed = Mock()
    construct_profile = Mock(return_value=profile)
    construct_judge = Mock(return_value=judge)
    construct_routed = Mock(return_value=routed)
    engine = Mock()
    engine.step.return_value = {**state, "stage": "review"}
    construct_runner = Mock(return_value=engine)
    monkeypatch.setattr(workflow_runner, "HeadlessProfile", construct_profile)
    monkeypatch.setattr(workflow_runner, "Judge", construct_judge)
    monkeypatch.setattr(workflow_runner, "Routed", construct_routed)
    monkeypatch.setattr(workflow_runner.runner, "Runner", construct_runner)
    inbox = Mock()
    assert workflow_runner.advance_task(inbox, row, tmp_path) == (
        "workflow work advanced from build to review: running"
    )
    construct_profile.assert_called_once_with(harness="codex")
    construct_judge.assert_called_once_with(harness="codex")
    seat = construct_routed.call_args.args[0]
    assert seat.inbox is inbox
    assert seat.task == row
    assert seat.definition == flow
    assert seat.fallback is profile
    construct_routed.assert_called_once_with(seat, judge)
    construct_runner.assert_called_once_with(flow, routed, workflow_runner.exit_checks.check)
    engine.step.assert_called_once_with(tmp_path)
