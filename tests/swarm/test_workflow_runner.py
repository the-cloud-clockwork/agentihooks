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
                    "exit": "review.pass",
                    "on_exhausted": "block",
                },
                "stages": {
                    "build": {"profile": "engineer", "input": ["case"], "next": {"pass": "review"}},
                    "review": {"profile": "judge", "input": ["build"], "next": {"pass": "exit", "retry": "build"}},
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


def test_pending_head_is_delivered_once_without_consuming_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import workflow_runner

    resolve(monkeypatch)
    inbox = Mock()
    row = task(tmp_path)
    doc = {"tasks": [row]}
    workflow_runner.advance_tasks(inbox, "proof", doc, tmp_path)
    before = store.load(tmp_path)
    assert (before["stage"], before["status"], before["visit"], before["runs"]) == ("build", "running", 0, {})
    assert before["case"] == "Build the case"
    assert before["definition"] == definition().to_document()
    assert before["harness"] == "auto"
    workflow_runner.advance_tasks(inbox, "proof", doc, tmp_path)
    assert store.load(tmp_path) == before
    assert inbox.send.call_count == 1
    args, kwargs = inbox.send.call_args
    assert args[:2] == ("swarm", row["claimed_by"])
    assert kwargs["task"] == "work"
    assert '"case": "Build the case"' in args[2]
    assert str(tmp_path / "outputs" / "build" / "seat-1-0-0.json") in args[2]


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
    workflow_runner.advance_tasks(inbox, "proof", doc, tmp_path)
    result = tmp_path / "outputs" / "build" / "seat-1-0-0.json"
    result.write_text(json.dumps({"outcome": "pass", "outputs": "built"}))
    workflow_runner.advance_tasks(inbox, "proof", doc, tmp_path)
    assert (store.load(tmp_path)["stage"], store.load(tmp_path)["outputs"]) == ("review", {"build": "built"})
    fallback.launch.assert_not_called()
    workflow_runner.advance_tasks(inbox, "proof", doc, tmp_path)
    fallback.launch.assert_called_once_with(
        definition().stages["review"], {"case": "Build the case", "build": "built"}, tmp_path
    )
    assert store.load(tmp_path)["status"] == "passed"
    workflow_runner.advance_tasks(inbox, "proof", doc, tmp_path)
    assert fallback.launch.call_count == 1


@pytest.mark.parametrize(
    "changes", [{"kind": "code"}, {"state": "open"}, {"state": "done"}, {"done": True}, {"claimed_by": ""}]
)
def test_only_claimed_workflow_tasks_run(tmp_path: Path, changes: dict) -> None:
    from scripts.swarm import workflow_runner

    inbox = Mock()
    assert workflow_runner.advance_tasks(inbox, "proof", {"tasks": [{**task(tmp_path), **changes}]}, tmp_path) == []
    assert not (tmp_path / "workflow.json").exists()
    inbox.send.assert_not_called()


def test_existing_state_and_case_survive_definition_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import workflow_runner

    flow = resolve(monkeypatch)
    case = tmp_path / "case.md"
    case.write_text("Original case")
    row = {**task(tmp_path), "case": str(case)}
    workflow_runner.advance_tasks(Mock(), "proof", {"tasks": [row]}, tmp_path)
    state = store.load(tmp_path)
    assert state["case"] == "Original case"
    case.write_text("Changed case")
    monkeypatch.setattr(
        workflow_runner.loader, "resolve", Mock(side_effect=AssertionError("must resume stored definition"))
    )
    workflow_runner.advance_tasks(Mock(), "proof", {"tasks": [row]}, tmp_path)
    assert store.load(tmp_path)["definition"] == flow.to_document()
    assert store.load(tmp_path)["case"] == "Original case"
    assert store.load(tmp_path) == state


def test_bad_task_does_not_prevent_the_next_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import workflow_runner

    resolve(monkeypatch)
    bad = {**task(tmp_path / "bad"), "workflow": "missing", "id": "bad"}
    good = {**task(tmp_path / "good"), "id": "good"}
    actions = workflow_runner.advance_tasks(Mock(), "proof", {"tasks": [bad, good]}, tmp_path)
    assert any("bad" in action and "missing" in action for action in actions)
    assert store.load(tmp_path / "good")["status"] == machine.RUNNING
