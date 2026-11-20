import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
PATTERN_DOWNLOAD = "./.workflow-revision/.github/actions/pattern-download"


def _jobs():
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def _first_attempts():
    for job, spec in _jobs().items():
        steps = spec.get("steps") or []
        for index, step in enumerate(steps):
            if step.get("uses", "").startswith(("actions/download-artifact@", PATTERN_DOWNLOAD)) and not step[
                "name"
            ].endswith(", attempt 2"):
                yield job, steps, index


def test_every_tests_job_downloading_artifacts_is_covered():
    jobs = {job for job, _, _ in _first_attempts()}
    assert {
        "select",
        "split",
        "unit",
        "unit-rerun",
        "unit-selected",
        "sonar",
        "coverage-ratchet",
        "refresh-durations",
        "flake-budget",
        "coverage-baseline",
        "mutation-stats",
        "mutation",
        "shard-check",
        "test-summary",
    } <= jobs


@pytest.mark.parametrize(
    "job, index",
    [(job, index) for job, _, index in _first_attempts()],
)
def test_a_failed_download_names_the_outage_waits_and_downloads_once_more(job, index, tmp_path):
    steps = _jobs()[job]["steps"]
    first, wait, second = steps[index : index + 3]
    assert first["continue-on-error"] is True
    retry = f"steps.{first['id']}.outcome == 'failure'"
    assert wait["if"] == second["if"] == retry
    assert wait["name"] == f"{first['name']}, wait before attempt 2"
    assert second["name"] == f"{first['name']}, attempt 2"
    assert second["uses"] == first["uses"]
    assert second["with"] == first["with"]
    assert "continue-on-error" not in second
    assert [s.get("id") for s in steps].count(first["id"]) == 1
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "sleep").write_text('#!/usr/bin/env bash\necho "$1" >> "$SLEPT"\n')
    (binary / "sleep").chmod(0o755)
    result = subprocess.run(
        [wait["shell"], "-e", "-c", wait["run"]],
        env=dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}", SLEPT=str(tmp_path / "slept")),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.startswith(f"::warning::{first['name']} failed, as an artifact service outage does;")
    assert (tmp_path / "slept").read_text() == "30\n"


def test_each_retried_shared_mutation_stats_download_gives_up_after_one_minute() -> None:
    attempts = [
        step
        for spec in yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"].values()
        for step in spec.get("steps") or []
        if step.get("name") in ("Download the shared mutation stats", "Download the shared mutation stats, attempt 2")
        and (step.get("continue-on-error") or step["name"].endswith("attempt 2"))
    ]
    assert len(attempts) == 6
    assert {step.get("timeout-minutes") for step in attempts} == {1}


@pytest.mark.feature("shared-mutation-stats-download-ends-with-its-download")
def test_every_shared_mutation_stats_download_runs_the_action_that_closes_each_blob_answer() -> None:
    downloads = {
        (job, step["name"]): (step["uses"], step["with"])
        for job, spec in _jobs().items()
        for step in spec.get("steps") or []
        if step.get("name", "").startswith(
            ("Download the shared mutation stats", "Download the missing mutation stats")
        )
        and "uses" in step
    }
    assert {job for job, _ in downloads} == {"mutation-refine", "mutation-probe", "mutation-shard"}
    assert len(downloads) == 8
    assert all(
        found == (PATTERN_DOWNLOAD, {"pattern": "mutation-stats-*", "path": ".mutation-stats"})
        for found in downloads.values()
    )


def _download_steps(node: object) -> list[str]:
    if isinstance(node, dict):
        uses = node.get("uses")
        found = [uses] if isinstance(uses, str) and uses.startswith("actions/download-artifact@") else []
        return found + [ref for value in node.values() for ref in _download_steps(value)]
    if isinstance(node, list):
        return [ref for value in node for ref in _download_steps(value)]
    return []


@pytest.mark.feature("artifact-downloads-retry-a-stalled-blob")
def test_every_download_step_uses_the_release_that_retries_a_stalled_blob() -> None:
    github = ROOT / ".github"
    files = [*github.rglob("*.yml"), *github.rglob("*.yaml"), ROOT / "scripts/ci_gate_summary_steps.yml"]
    refs = {str(path.relative_to(ROOT)): _download_steps(yaml.safe_load(path.read_text())) for path in files}
    written = sum(len(re.findall(r"uses:\s*['\"]?actions/download-artifact@", path.read_text())) for path in files)
    assert written and sum(len(found) for found in refs.values()) >= written
    assert {path: found for path, found in refs.items() if set(found) - {"actions/download-artifact@v8.0.2"}} == {}
