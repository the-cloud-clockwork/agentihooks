import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("scripts/hive/daemon.py", "true"),
        ("scripts/hive/dispatch.py", "true"),
        ("scripts/hive/orders.py", "true"),
        ("scripts/packaging/hive-join-smoke.sh", "true"),
        (".github/workflows/swarm-smoke.yml", "true"),
        (".github/workflows/test.yml", "true"),
        ("README.md", "false"),
        ("scripts/hive/other.py", "false"),
    ],
)
def test_hive_join_selection(path: str, expected: str, tmp_path: Path) -> None:
    steps = workflow("writer-lock")["jobs"]["inputs"]["steps"]
    run = next(step["run"] for step in steps if step.get("id") == "hive-join")
    paths = tmp_path / "hive-join-paths"
    paths.write_bytes(path.encode() + b"\0")
    output = tmp_path / "output"
    git = tmp_path / "git"
    git.write_text(
        '#!/usr/bin/env bash\nif [[ "$*" == *merge-base* ]]; then echo base; else cat "$RUNNER_TEMP/hive-join-paths"; fi\n'
    )
    git.chmod(0o755)
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", run],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "PR_BASE": "base",
            "PR_HEAD": "head",
        },
        check=True,
    )
    assert output.read_text() == f"due={expected}\n"


def test_hive_join_is_required_only_for_selected_pull_requests() -> None:
    jobs = workflow("test")["jobs"]
    assert "writer-lock" in jobs["gate-required"]["needs"]
    assert "github.event_name == 'pull_request'" in jobs["writer-lock"]["if"]
    smoke_jobs = workflow("writer-lock")["jobs"]
    caller = smoke_jobs["hive-join"]
    assert caller["needs"] == ["inputs"]
    assert "github.event_name == 'pull_request'" in caller["if"]
    assert "needs.inputs.outputs.hive_join == 'true'" in caller["if"]
    assert caller["uses"] == "./.github/workflows/swarm-smoke.yml"
    assert caller["with"]["hive-join-only"] is True
    assert smoke_jobs["inputs"]["outputs"]["hive_join"] == "${{ steps.hive-join.outputs.due }}"
    assert "hive-join" not in smoke_jobs["scenario"]["needs"]


def test_hive_join_mode_preserves_promotion_scenarios() -> None:
    smoke = workflow("swarm-smoke")
    inputs = smoke.get("on", smoke.get(True))["workflow_call"]["inputs"]
    assert inputs["hive-join-only"] == {"type": "boolean", "default": False}
    steps = smoke["jobs"]["smoke"]["steps"]
    focused = next(step for step in steps if step.get("name") == "Run the hive join scenario")
    assert "inputs.hive-join-only" in focused["if"]
    assert focused["run"] == "bash scripts/packaging/hive-join-smoke.sh"
    full = next(step for step in steps if "scenarios in parallel" in step.get("name", ""))
    assert "!inputs.hive-join-only" in full["if"]
    assert "scripts/packaging/hive-join-smoke.sh" in full["run"]
