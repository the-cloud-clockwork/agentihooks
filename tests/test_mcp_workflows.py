"""The workflows MCP category: list, run, status, schedule and unschedule."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import hooks.mcp.workflows as workflows
from scripts.events.redis_state import RedisStateStore
from scripts.scheduler import commands
from scripts.workflows import cli as workflow_cli
from scripts.workflows import store
from scripts.workflows.runner import StageResult

pytestmark = pytest.mark.unit

NOW = 1000.0
SID = "sid-caller"
WORKFLOW = """\
workflow:
  name: sample
  intent: Build the sample
  head: build
  exit: build.pass
  max_iterations: 1
stages:
  build:
    profile: engineer
    next: {pass: exit, retry: exit, enhance: exit}
"""


class Collector:
    """A stand in server that keeps each registered tool by name."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., str]] = {}

    def tool(self) -> Callable[[Callable[..., str]], Callable[..., str]]:
        def keep(fn: Callable[..., str]) -> Callable[..., str]:
            self.tools[fn.__name__] = fn
            return fn

        return keep


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    (folder / ".agentihooks" / "workflows").mkdir(parents=True)
    (folder / ".agentihooks" / "workflows" / "sample.yml").write_text(WORKFLOW)
    monkeypatch.chdir(folder)
    monkeypatch.delenv("AGENTIHOOKS_WORKER_RUN", raising=False)
    monkeypatch.setattr(store, "default_root", lambda: tmp_path / "runs")
    return folder


@pytest.fixture
def launch(monkeypatch: pytest.MonkeyPatch) -> Mock:
    found = Mock(return_value=StageResult("pass", ["done"]))
    monkeypatch.setattr(workflow_cli, "HeadlessProfile", Mock(return_value=Mock(launch=found)))
    return found


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> Any:
    import fakeredis

    from scripts.inbox import cli as inbox_cli
    from scripts.scheduler.core import Scheduler

    found = Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof")
    monkeypatch.setattr(commands, "connect", lambda: found)
    monkeypatch.setattr(commands.time, "time", lambda: NOW)
    monkeypatch.setattr(inbox_cli, "registered_name", lambda: "")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    return found


@pytest.fixture
def tools() -> dict[str, Callable[..., Any]]:
    collector = Collector()
    workflows.register(collector)
    return {name: lambda *a, fn=fn, **k: json.loads(fn(*a, **k)) for name, fn in collector.tools.items()}


def test_the_workflows_category_registers_every_tool(tools: dict[str, Callable[..., Any]]) -> None:
    assert set(tools) == {
        "workflow_list",
        "workflow_run",
        "workflow_status",
        "workflow_schedule",
        "workflow_unschedule",
    }


def test_workflow_list_names_each_workflow_with_its_layer_and_file(
    tools: dict[str, Callable[..., Any]], project: Path
) -> None:
    listed = tools["workflow_list"]()
    sample = next(item for item in listed["workflows"] if item["name"] == "sample")
    assert listed["success"] is True
    assert sample == {
        "name": "sample",
        "layer": "project",
        "path": str(project / ".agentihooks" / "workflows" / "sample.yml"),
    }


def test_workflow_run_waits_for_the_finished_state_and_status_reads_it_back(
    tools: dict[str, Callable[..., Any]], project: Path, launch: Mock
) -> None:
    state = tools["workflow_run"]("sample", "Build the sample")

    assert (state["status"], state["workflow"], state["outputs"]) == ("passed", "sample", {"build": ["done"]})
    (stage, inputs, _), _ = launch.call_args
    assert (stage.id, inputs) == ("build", {"case": "Build the sample"})
    assert tools["workflow_status"](state["run"]) == state


def test_workflow_run_without_wait_hands_the_saved_run_to_a_detached_resume(
    tools: dict[str, Callable[..., Any]], project: Path, launch: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    detached: list[str] = []
    monkeypatch.setattr(workflows, "detach", detached.append)

    state = tools["workflow_run"]("sample", "Build the sample", wait=False)

    assert state["status"] == "running"
    assert detached == [state["run"]]
    launch.assert_not_called()


def test_workflow_run_refuses_an_unknown_workflow_and_a_nested_run(
    tools: dict[str, Callable[..., Any]], project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tools["workflow_run"]("ghost", "x") == {"success": False, "error": "no workflow named ghost"}
    monkeypatch.setenv("AGENTIHOOKS_WORKER_RUN", "1")
    assert tools["workflow_run"]("sample", "x") == {"success": False, "error": workflows.NESTED}


def test_workflow_status_refuses_a_run_identifier_that_is_a_path(tools: dict[str, Callable[..., Any]]) -> None:
    assert tools["workflow_status"]("../x") == {"success": False, "error": "run identifier must be a single name"}


def test_workflow_schedule_arms_a_cron_and_unschedule_cancels_it(
    tools: dict[str, Callable[..., Any]], scheduler: Any, tmp_path: Path
) -> None:
    case = tmp_path / "case.md"
    case.write_text("Build the sample")

    answer = tools["workflow_schedule"]("sample", str(case), expression="*/5 * * * *")
    entry = answer["entry"]

    assert answer["success"] is True
    assert (entry["kind"], entry["owner"], entry["payload"], entry["due"]) == ("cron", SID, "workflow sample", 1200.0)
    assert entry["targets"] == [{"kind": "workflow", "name": "sample", "case": str(case.resolve())}]
    assert tools["workflow_unschedule"](entry["id"]) == {"success": True, "entry": {**entry, "state": "cancelled"}}
    assert scheduler.get(entry["id"]).state == "cancelled"


def test_workflow_schedule_arms_a_timer_after_a_delay(
    tools: dict[str, Callable[..., Any]], scheduler: Any, tmp_path: Path
) -> None:
    case = tmp_path / "case.md"
    case.write_text("Build the sample")

    entry = tools["workflow_schedule"]("sample", str(case), when="5m", agent_name="eng-1@sw")["entry"]
    timed = tools["workflow_schedule"]("sample", str(case), when="2026-10-18T09:30:00+00:00")["entry"]

    assert (entry["kind"], entry["owner"], entry["due"]) == ("timer", "eng-1@sw", NOW + 300)
    assert (timed["kind"], timed["owner"], timed["due"]) == ("timer", SID, 1792315800.0)
    assert tools["workflow_schedule"]("sample", str(case), when="later") == {
        "success": False,
        "error": "later is not an ISO time with an offset such as 2026-10-18T09:30:00+00:00",
    }


def test_workflow_schedule_needs_exactly_one_moment_and_a_case_file(
    tools: dict[str, Callable[..., Any]], scheduler: Any, tmp_path: Path
) -> None:
    case = tmp_path / "case.md"
    case.write_text("x")
    one = {"success": False, "error": "give one of expression or when"}
    assert tools["workflow_schedule"]("sample", str(case)) == one
    assert tools["workflow_schedule"]("sample", str(case), expression="* * * * *", when="5m") == one
    missing = str(tmp_path / "missing.md")
    assert tools["workflow_schedule"]("sample", missing, when="5m") == {
        "success": False,
        "error": f"{missing} is not a case file",
    }


def test_workflow_unschedule_refuses_a_schedule_that_is_not_a_workflow(
    tools: dict[str, Callable[..., Any]], scheduler: Any
) -> None:
    other = commands.add_timer(scheduler, SID, commands.target_slot("fleet"), "hello", delay=60)
    assert tools["workflow_unschedule"](other["id"]) == {
        "success": False,
        "error": f"no workflow schedule {other['id']}",
    }
    assert tools["workflow_unschedule"]("nope") == {"success": False, "error": "no workflow schedule nope"}
    assert scheduler.get(other["id"]).state == "active"
