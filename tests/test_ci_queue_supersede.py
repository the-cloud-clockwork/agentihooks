import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
STEP = "Cancel older queue runs of the same pull request"
RUN = 500
QUEUE = "gh-readonly-queue/dev/pr-123-"
RUNS = {
    "queued": [(410, QUEUE + "aaa111"), (420, "gh-readonly-queue/dev/pr-1234-bbb222")],
    "in_progress": [(430, QUEUE + "ccc333"), (510, QUEUE + "ddd444"), (440, "gh-readonly-queue/dev/pr-12-eee555")],
    "waiting": [(450, QUEUE + "not-a-sha")],
}

GH = """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1 $2" == "api -X" ]]; then
  [[ -z "${FAIL_CANCEL:-}" ]] || exit 1
  echo "$4" >> "$CANCELLED"
  exit 0
fi
[[ -z "${FAIL_LIST:-}" ]] || exit 1
for arg in "$@"; do
  if [[ "$arg" =~ status=([a-z_]+) ]]; then
    cat "$FIXTURES/${BASH_REMATCH[1]}" 2>/dev/null || true
  fi
done
"""


def _workflow():
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())


def _step():
    return next(step for step in _workflow()["jobs"]["reuse"]["steps"] if step.get("name") == STEP)


def _run(tmp_path, ref, **env):
    fixtures, bin_dir = tmp_path / "fixtures", tmp_path / "bin"
    fixtures.mkdir()
    bin_dir.mkdir()
    for status, runs in RUNS.items():
        (fixtures / status).write_text("".join(f"{run} {branch}\n" for run, branch in runs))
    gh = bin_dir / "gh"
    gh.write_text(GH)
    gh.chmod(0o755)
    cancelled = tmp_path / "cancelled"
    cancelled.touch()
    result = subprocess.run(
        ["bash", "-e", "-c", _step()["run"]],
        env=dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            FIXTURES=str(fixtures),
            CANCELLED=str(cancelled),
            QUEUE_REF=ref,
            RUN=str(RUN),
            GITHUB_REPOSITORY="owner/repo",
            **env,
        ),
        capture_output=True,
        text=True,
    )
    return result, cancelled.read_text().split()


def test_pull_request_and_push_concurrency_stays_unchanged():
    concurrency = _workflow()["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert concurrency["queue"] == "${{ github.event_name == 'workflow_dispatch' && 'max' || 'single' }}"
    assert concurrency["group"].endswith(
        "|| format('tests-{0}-{1}', github.event_name, github.event.pull_request.number || github.run_id) }}"
    )


def test_the_supersede_step_runs_first_and_only_in_the_merge_queue():
    job = _workflow()["jobs"]["reuse"]
    step = _step()
    assert job["steps"][0] == step
    assert step["if"] == "github.event_name == 'merge_group'"
    assert step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "QUEUE_REF": "${{ github.event.merge_group.head_ref }}",
        "RUN": "${{ github.run_id }}",
    }
    assert job["permissions"] == {"contents": "read", "actions": "write"}


def test_only_older_runs_of_the_same_pull_request_are_cancelled(tmp_path):
    result, cancelled = _run(tmp_path, "refs/heads/" + QUEUE + "fff666")
    assert result.returncode == 0, result.stdout + result.stderr
    assert cancelled == [
        "repos/owner/repo/actions/runs/410/cancel",
        "repos/owner/repo/actions/runs/430/cancel",
    ]
    assert "Cancelled superseded queue run 410 (gh-readonly-queue/dev/pr-123-aaa111)" in result.stdout


def test_a_ref_outside_the_merge_queue_cancels_nothing(tmp_path):
    result, cancelled = _run(tmp_path, "refs/heads/dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert cancelled == []


@pytest.mark.parametrize("failure", ["FAIL_LIST", "FAIL_CANCEL"])
def test_a_failed_listing_or_cancel_warns_without_failing_the_live_run(tmp_path, failure):
    result, cancelled = _run(tmp_path, "refs/heads/" + QUEUE + "fff666", **{failure: "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert cancelled == []
    assert "::warning::" in result.stdout
