"""The mutation plan job collects four stats parts or fewer itself, in the shape the shard and gate jobs read."""

import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from scripts.ci_mutation.arrival import missing_producers

_ROOT = Path(__file__).resolve().parents[1]


def _plan_steps() -> list[dict[str, Any]]:
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["mutation-plan"]["steps"]


def test_the_plan_job_marks_every_collected_stats_part_for_the_shard_arrival_check(tmp_path: Path) -> None:
    step = next(step for step in _plan_steps() if step.get("id") == "stats")
    script = step["run"].replace("python -m scripts.ci_mutation ", "true ")
    env = {**os.environ, "PARTS": "[0, 1, 2]", "RUNNER_TEMP": str(tmp_path), "BASES": "origin/dev"}
    subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=tmp_path, env=env, check=True)
    assert missing_producers(tmp_path / ".mutation-stats", [0, 1, 2]) == []


def test_the_plan_job_takes_every_local_action_before_the_stats_setup_uses_one() -> None:
    steps = _plan_steps()
    names = [step.get("name") for step in steps]
    take = steps[names.index("Take the local actions from the workflow revision for the stats")]
    assert take["run"] == 'git archive "$GITHUB_SHA" .github/actions | tar -x -C .workflow-revision'
    setup = [step for step in steps if str(step.get("uses", "")).endswith(("nats-server", "browser-cache"))]
    assert len(setup) == 2
    assert all(steps.index(take) < steps.index(step) for step in setup)
    assert names[-2:] == ["Grade the empty change set", "Forward the empty mutation report"]


def test_the_plan_job_uses_only_its_local_actions_and_takes_shards_from_refine_first() -> None:
    plan = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["mutation-plan"]
    local = {step["uses"] for step in plan["steps"] if str(step.get("uses", "")).startswith("./.")}
    assert local == {
        "./.workflow-revision/.github/actions/nats-server",
        "./.workflow-revision/.github/actions/browser-cache",
        "./.workflow-revision/.github/actions/retry-upload",
    }
    assert plan["outputs"]["shards"] == "${{ steps.refine.outputs.shards || steps.plan.outputs.shards }}"
