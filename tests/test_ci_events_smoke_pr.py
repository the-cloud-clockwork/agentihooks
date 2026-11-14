import ast
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "tests/events_smoke/test_launch_path.py"
SEAMS = {
    "test_a_pull_request_task_is_admitted_well_under_the_hive_wait_with_no_live_github_listing",
    "test_the_admitted_agent_record_carries_the_harness_account_model_and_effort_the_hive_launched",
    "test_a_claimed_task_with_no_live_agent_is_reopened_by_the_tick_on_the_events_engine",
    "test_a_hive_at_its_max_agents_takes_no_admission_and_a_hive_below_it_takes_the_offer",
    "test_an_engine_switch_exits_no_running_execution",
    "test_no_legacy_launch_path_runs_while_the_spoke_launches_the_work",
}


def workflow(name: str) -> dict[Any, Any]:
    """Load one workflow file."""
    return yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())


@pytest.mark.feature("events-smoke-pull-request-tier")
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("scripts/hub/admission.py", "true"),
        ("scripts/hub/hive_loss.py", "true"),
        ("scripts/hive/spoke.py", "true"),
        ("scripts/hive/lanes.py", "true"),
        ("scripts/tick/steps/agents.py", "true"),
        ("scripts/tick/engine.py", "true"),
        ("tests/events_smoke/test_launch_path.py", "true"),
        (".github/workflows/events-smoke.yml", "true"),
        (".github/workflows/writer-lock.yml", "true"),
        ("README.md", "false"),
        ("scripts/swarm/store.py", "false"),
        ("tests/hub/test_admission.py", "false"),
    ],
)
def test_events_smoke_selection(path: str, expected: str, tmp_path: Path) -> None:
    steps = workflow("writer-lock")["jobs"]["inputs"]["steps"]
    step = next(step for step in steps if step.get("id") == "events")
    paths = tmp_path / "events-paths"
    paths.write_bytes(path.encode() + b"\0")
    output = tmp_path / "output"
    git = tmp_path / "git"
    git.write_text(
        '#!/usr/bin/env bash\nif [[ "$*" == *merge-base* ]]; then echo base; else cat "$RUNNER_TEMP/events-paths"; fi\n'
    )
    git.chmod(0o755)
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "PR_BASE": "base",
            "PR_HEAD": "head",
            "EVENTS_INPUTS": step["env"]["EVENTS_INPUTS"],
        },
        check=True,
    )
    assert output.read_text() == f"due={expected}\n"


@pytest.mark.feature("events-smoke-pull-request-tier")
def test_the_events_smoke_feeds_the_required_gate_on_selected_pull_requests() -> None:
    tests = workflow("test")["jobs"]
    assert "writer-lock" in tests["gate-required"]["needs"]
    assert "github.event_name == 'pull_request'" in tests["writer-lock"]["if"]
    lock = workflow("writer-lock")["jobs"]
    caller = lock["events"]
    assert caller["needs"] == ["inputs"]
    assert caller["if"] == "github.event_name == 'pull_request' && needs.inputs.outputs.events == 'true'"
    assert caller["uses"] == "./.github/workflows/events-smoke.yml"
    assert lock["inputs"]["outputs"]["events"] == "${{ steps.events.outputs.due }}"


@pytest.mark.feature("events-smoke-pull-request-tier")
def test_the_events_smoke_runs_every_seam_on_a_real_nats_server_and_redis_service() -> None:
    called = workflow("events-smoke")
    assert "workflow_call" in called[True]
    job = called["jobs"]["smoke"]
    assert job["services"]["redis"]["image"].endswith("/redis:7-alpine")
    assert job["env"]["AGENTIHOOKS_EVENTS_SMOKE"] == "1"
    assert job["env"]["AGENTIHOOKS_EVENTS_SMOKE_REDIS_URL"] == "redis://127.0.0.1:6381/0"
    steps = job["steps"]
    assert any(step.get("uses") == "./.github/actions/nats-server" for step in steps)
    [run] = [step for step in steps if "tests/events_smoke" in step.get("run", "")]
    assert "if" not in run and "continue-on-error" not in run and "|| true" not in run["run"]
    assert "--confcutdir=tests/events_smoke" in run["run"]
    module = ast.parse(SMOKE.read_text())
    found = {node.name for node in module.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test")}
    assert found == SEAMS
    assert f"grep -q ' {len(SEAMS)} passed'" in run["run"]
