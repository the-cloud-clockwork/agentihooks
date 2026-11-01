"""Tick guards run on every pull request that touches the tick and feed the required gate."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def workflow(name: str) -> dict:
    """Load one workflow file."""
    return yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("scripts/tick/engine.py", "true"),
        ("scripts/tick/registry.py", "true"),
        ("scripts/tick/steps/tick_reaper.py", "true"),
        ("scripts/tick/adapters/ledger.py", "true"),
        ("scripts/swarm/tick.py", "true"),
        ("scripts/swarm/tick_reaper.py", "true"),
        ("scripts/swarm/scheduled_tick.py", "true"),
        ("tests/swarm/tick_scenarios/goldens.json", "true"),
        ("tests/swarm/tick_equivalence.sh", "true"),
        ("tests/swarm/test_tick_load.py", "true"),
        (".github/workflows/tick-equivalence.yml", "true"),
        ("README.md", "false"),
        ("scripts/swarm/store.py", "false"),
        ("tests/swarm/test_tick.py", "false"),
    ],
)
def test_tick_guard_selection(path: str, expected: str, tmp_path: Path) -> None:
    steps = workflow("writer-lock")["jobs"]["inputs"]["steps"]
    step = next(step for step in steps if step.get("id") == "tick")
    paths = tmp_path / "tick-paths"
    paths.write_bytes(path.encode() + b"\0")
    output = tmp_path / "output"
    git = tmp_path / "git"
    git.write_text(
        '#!/usr/bin/env bash\nif [[ "$*" == *merge-base* ]]; then echo base; else cat "$RUNNER_TEMP/tick-paths"; fi\n'
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
            "TICK_INPUTS": step["env"]["TICK_INPUTS"],
        },
        check=True,
    )
    assert output.read_text() == f"due={expected}\n"


def test_tick_guards_feed_the_required_gate_on_selected_pull_requests() -> None:
    tests = workflow("test")["jobs"]
    assert "writer-lock" in tests["gate-required"]["needs"]
    assert "github.event_name == 'pull_request'" in tests["writer-lock"]["if"]
    lock = workflow("writer-lock")["jobs"]
    caller = lock["tick"]
    assert caller["needs"] == ["inputs"]
    assert caller["if"] == "github.event_name == 'pull_request' && needs.inputs.outputs.tick == 'true'"
    assert caller["uses"] == "./.github/workflows/tick-equivalence.yml"
    assert lock["inputs"]["outputs"]["tick"] == "${{ steps.tick.outputs.due }}"


def test_tick_equivalence_runs_characterisation_equivalence_and_load_budget() -> None:
    called = workflow("tick-equivalence")
    assert "workflow_call" in called[True]
    steps = called["jobs"]["tick-equivalence"]["steps"]
    runs = [step.get("run", "") for step in steps]
    assert "bash head/tests/swarm/tick_equivalence.sh" in runs
    [load] = [run for run in runs if "test_tick_load.py" in run]
    assert "refs/remotes/origin/dev" in load
    assert all(
        "if" not in step for step in steps if step.get("run") in (load, "bash head/tests/swarm/tick_equivalence.sh")
    )
    script = (ROOT / "tests/swarm/tick_equivalence.sh").read_text()
    assert "test_tick_matches_golden" in script
    assert "diff -ru tick-evidence/base tick-evidence/head" in script
