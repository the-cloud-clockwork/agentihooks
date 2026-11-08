"""The agents MCP category: profile cards and one-stage workflow runs."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import hooks.mcp.agents as agents
from scripts.agents import cli as agents_cli
from scripts.workflows import cli as workflow_cli
from scripts.workflows import store
from scripts.workflows.runner import StageResult

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("mcp-sdk")]

CARDS = {
    "engineer": {"name": "engineer", "description": "Builds", "capabilities": ["code"]},
    "scouter": {"name": "scouter", "description": "Finds", "capabilities": ["search", "code"]},
    "judge": {"name": "judge", "description": "Rules", "capabilities": []},
}


@pytest.fixture
def cards(monkeypatch: pytest.MonkeyPatch) -> None:
    def show(name: str) -> dict[str, Any]:
        if name not in CARDS:
            raise ValueError(f"Unknown agent profile: {name}")
        return CARDS[name]

    monkeypatch.setattr(agents_cli, "show_card", show)
    monkeypatch.setattr(agents_cli.install, "_available_profiles", lambda: list(CARDS))


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cards: None) -> tuple[Path, Mock, Mock]:
    monkeypatch.delenv("AGENTIHOOKS_WORKER_RUN", raising=False)
    root = tmp_path / "runs"
    monkeypatch.setattr(store, "default_root", lambda: root)
    launch = Mock(return_value=StageResult("pass", ["done"]))
    factory = Mock(return_value=Mock(launch=launch))
    monkeypatch.setattr(workflow_cli, "HeadlessProfile", factory)
    return root, launch, factory


def test_agent_list_serves_every_card_and_filters_by_capability(cards: None) -> None:
    assert json.loads(agents.agent_list()) == list(CARDS.values())
    assert json.loads(agents.agent_list(capability="code")) == [CARDS["engineer"], CARDS["scouter"]]


def test_agent_show_serves_one_card_and_refuses_an_unknown_profile(cards: None) -> None:
    assert json.loads(agents.agent_show("judge")) == CARDS["judge"]
    assert json.loads(agents.agent_show("ghost")) == {"success": False, "error": "Unknown agent profile: ghost"}


def test_agent_run_with_wait_runs_one_stage_and_returns_the_finished_state(runs: tuple[Path, Mock, Mock]) -> None:
    root, launch, factory = runs

    state = json.loads(agents.agent_run("engineer", "Build the sample"))

    assert state["status"] == "passed"
    assert state["runs"] == {"run": 1}
    assert state["outputs"] == {"run": ["done"]}
    assert state["workflow"] == "agent-engineer"
    assert store.load(root / state["run"]) == state
    factory.assert_called_once_with(harness="auto")
    (stage, inputs, folder), _ = launch.call_args
    assert (stage.id, stage.profile, stage.count) == ("run", "engineer", 1)
    assert inputs == {"case": "Build the sample"}
    assert folder == root / state["run"]


def test_agent_run_that_fails_its_stage_ends_exhausted_after_one_launch(runs: tuple[Path, Mock, Mock]) -> None:
    _, launch, _ = runs
    launch.return_value = StageResult("retry", ["broken"])

    state = json.loads(agents.agent_run("engineer", "Build the sample"))

    assert state["status"] == "exhausted"
    assert state["action"] == "block"
    assert launch.call_count == 1


def test_agent_run_without_wait_resumes_in_a_detached_process(
    runs: tuple[Path, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launch, _ = runs
    popen = Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)

    state = json.loads(agents.agent_run("scouter", "Find the cause", wait=False))

    assert state["status"] == "running"
    assert state["stage"] == "run"
    assert store.load(root / state["run"]) == state
    launch.assert_not_called()
    (argv,), options = popen.call_args
    assert argv == [sys.executable, "-m", "scripts.install", "workflow", "resume", state["run"]]
    assert options["start_new_session"] is True
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL


def test_agent_run_refuses_an_unknown_profile_before_saving_a_run(runs: tuple[Path, Mock, Mock]) -> None:
    root, launch, _ = runs

    result = json.loads(agents.agent_run("ghost", "anything"))

    assert result == {"success": False, "error": "Unknown agent profile: ghost"}
    assert not root.exists()
    launch.assert_not_called()


def test_agent_run_is_refused_inside_a_worker_run(
    runs: tuple[Path, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, launch, _ = runs
    monkeypatch.setenv("AGENTIHOOKS_WORKER_RUN", "1")

    result = json.loads(agents.agent_run("engineer", "Nest a run"))

    assert result == {"success": False, "error": agents.NESTED}
    assert not root.exists()
    launch.assert_not_called()


def test_the_server_serves_the_agents_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CATEGORIES", "agents")
    from hooks.mcp import build_server

    assert {"agent_list", "agent_show", "agent_run"} <= set(build_server()._tool_manager._tools)
