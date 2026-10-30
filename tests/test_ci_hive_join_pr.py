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
    step = next(step for step in steps if step.get("id") == "hive-join")
    run = step["run"]
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
            "HIVE_JOIN_INPUTS": step["env"]["HIVE_JOIN_INPUTS"],
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
    assert caller["uses"] == "./.github/workflows/hive-join.yml"
    assert smoke_jobs["inputs"]["outputs"]["hive_join"] == "${{ steps.hive-join.outputs.due }}"
    assert "hive-join" not in smoke_jobs["scenario"]["needs"]


def test_hive_join_mode_preserves_promotion_scenarios() -> None:
    focused = workflow("hive-join")["jobs"]["smoke"]
    steps = focused["steps"]
    scenario = next(step for step in steps if step.get("name") == "Run the hive join scenario")
    assert scenario["run"] == "bash scripts/packaging/hive-join-smoke.sh"
    assert "if" not in scenario
    assert focused["env"]["SWARM_SMOKE_RUNTIME_IMAGES_READY"] == "true"
    restore = next(step for step in steps if step.get("id") == "layers")
    full_steps = workflow("swarm-smoke")["jobs"]["smoke"]["steps"]
    shared_restore = next(step for step in full_steps if step.get("id") == "layers")
    assert restore["with"] == shared_restore["with"]
    build = next(step["run"] for step in steps if "docker build" in step.get("run", ""))
    assert '--cache-from "$LAYERS_FROM"' in build
    assert 'wait "$build" || status=$?' in build
    assert "redis:7-alpine" in build
    assert "nats:" not in build
    full = next(step for step in full_steps if "scenarios in parallel" in step.get("name", ""))
    assert "scripts/packaging/hive-join-smoke.sh" in full["run"]
    caller = workflow("test")["jobs"]["swarm-image"]
    assert caller["uses"] == "./.github/workflows/swarm-smoke.yml"
    assert "github.event_name == 'push'" in caller["if"]
