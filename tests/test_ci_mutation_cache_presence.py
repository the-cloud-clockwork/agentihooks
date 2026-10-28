import os
import subprocess
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[1]


def _steps():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["mutation"]["steps"]


def test_mutation_cache_save_uses_a_presence_output_without_hashing_files():
    steps = _steps()
    save = next(step for step in steps if step.get("name") == "Save the merged test stats")
    assert "hashFiles" not in save["if"]
    probe = next(step for step in steps if step.get("id") == "merged-stats")
    eligibility = (
        "strategy.job-index == 0 && needs.mutation-plan.outputs.stats_parts != '[0]'"
        " && needs.mutation-plan.outputs.forwarded != 'true'"
    )
    assert probe["if"] == eligibility
    assert save["if"] == f"{eligibility} && steps.merged-stats.outputs.found == 'true'"
    cap = next(step for step in steps if step.get("name") == "Cap the merged test stats")
    assert steps.index(cap) < steps.index(probe) < steps.index(save)
    assert probe["shell"] == "bash"
    assert probe["timeout-minutes"] == 1


@pytest.mark.parametrize("cache_state", ["missing", "empty", "nested", "large"])
def test_mutation_cache_presence_step_reports_files_without_reading_contents(tmp_path, cache_state):
    probe = next(step for step in _steps() if step.get("id") == "merged-stats")
    cache = tmp_path / ".mutation-stats-cache"
    if cache_state != "missing":
        (cache / "nested").mkdir(parents=True)
    if cache_state in ("nested", "large"):
        with (cache / "nested" / "stats.json").open("wb") as stream:
            stream.truncate(16 * 1024**3 if cache_state == "large" else 1)
    output = tmp_path / "output"
    output.touch()
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", probe["run"]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == ("found=true\n" if cache_state in ("nested", "large") else "")
