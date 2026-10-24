import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_ci_anton_runners import event, resolve

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
STEP = "Cancel superseded and dead merge queue runs"
RUN = 500
QUEUE = "gh-readonly-queue/dev/pr-123-"
OWN = QUEUE + "fff666"
RUNS = {
    "queued": [(410, QUEUE + "aaa111"), (420, "gh-readonly-queue/dev/pr-1234-bbb222")],
    "in_progress": [
        (430, QUEUE + "ccc333"),
        (RUN, OWN),
        (510, QUEUE + "ddd444"),
        (440, "gh-readonly-queue/dev/pr-12-eee555"),
        (480, "gh-readonly-queue/dev/pr-77-abc777"),
        (520, "gh-readonly-queue/dev/pr-88-abc888"),
    ],
    "waiting": [(450, QUEUE + "not-a-sha"), (455, QUEUE + "0a0b0c")],
    "pending": [(460, QUEUE + "1a1b1c")],
    "requested": [(470, QUEUE + "2a2b2c")],
}
LIVE = [
    OWN,
    QUEUE + "ddd444",
    QUEUE + "not-a-sha",
    "gh-readonly-queue/dev/pr-1234-bbb222",
    "gh-readonly-queue/dev/pr-12-eee555",
]

GH = r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1 $2" == "api -X" ]]; then
  [[ -z "${FAIL_CANCEL:-}" ]] || exit 1
  echo "$4" >> "$CANCELLED"
  exit 0
fi
runs='^repos/owner/repo/actions/workflows/test\.yml/runs\?event=merge_group&status=([a-z_]+)&per_page=100$'
refs='^repos/owner/repo/git/matching-refs/heads/gh-readonly-queue/$'
url="" filter=""
while (($#)); do
  case "$1" in
    --jq) filter="$2"; shift ;;
    repos/*) url="$1" ;;
  esac
  shift
done
echo "$url" >> "$URLS"
if [[ "$url" =~ $refs ]]; then
  [[ -z "${FAIL_REFS:-}" ]] || exit 1
  jq -r "$filter" "$FIXTURES/refs"
  exit 0
fi
[[ -z "${FAIL_LIST:-}" ]] || exit 1
[[ "$url" =~ $runs && -f "$FIXTURES/${BASH_REMATCH[1]}" ]] || exit 0
jq -r "$filter" "$FIXTURES/${BASH_REMATCH[1]}"
"""


def _workflow():
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())


def _step():
    [step] = [step for step in _workflow()["jobs"]["durations"]["steps"] if step.get("name") == STEP]
    return step


def _run(tmp_path, ref="refs/heads/" + OWN, live=LIVE, **env):
    fixtures, bin_dir = tmp_path / "fixtures", tmp_path / "bin"
    fixtures.mkdir()
    bin_dir.mkdir()
    for status, runs in RUNS.items():
        runs = [{"id": run, "head_branch": branch, "status": status} for run, branch in runs]
        (fixtures / status).write_text(json.dumps({"total_count": len(runs), "workflow_runs": runs}))
    (fixtures / "refs").write_text(json.dumps([{"ref": f"refs/heads/{branch}"} for branch in live]))
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
            URLS=str(tmp_path / "urls"),
            QUEUE_REF=ref,
            RUN=str(RUN),
            GITHUB_REPOSITORY="owner/repo",
            **env,
        ),
        capture_output=True,
        text=True,
    )
    return result, [int(path.split("/")[-2]) for path in cancelled.read_text().split()]


def test_the_workflow_concurrency_stays_unchanged():
    concurrency = _workflow()["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert concurrency["queue"] == "${{ github.event_name == 'workflow_dispatch' && 'max' || 'single' }}"
    assert concurrency["group"].endswith(
        "|| format('tests-{0}-{1}', github.event_name, github.event.pull_request.number || github.run_id) }}"
    )


@pytest.mark.parametrize(
    ("github", "runs"),
    [
        (event("merge_group"), True),
        (event("merge_group", actor="agenticore-app[bot]"), True),
        (event("merge_group", actor="github-merge-queue[bot]"), True),
        (event("merge_group", actor="github-merge-queue[bot]", triggering="nestorcolt"), True),
        (event("merge_group", actor="stranger"), False),
        (event("merge_group", triggering="stranger"), False),
        (event("merge_group", actor="github-merge-queue[bot]", triggering="stranger"), False),
        (event("pull_request"), False),
        (event("push", actor="github-merge-queue[bot]", ref="refs/heads/dev"), False),
    ],
)
def test_the_sweep_runs_on_merge_group_runs_from_trusted_queue_actors(github, runs):
    assert resolve("${{ " + _step()["if"] + " }}", **github) is runs


def test_the_sweep_runs_on_the_app_token_minted_to_cancel():
    job = _workflow()["jobs"]["durations"]
    names = [step.get("name") for step in job["steps"]]
    mint = job["steps"][names.index("Mint the tcc main ci App token")]
    assert names.index(STEP) == names.index("Mint the tcc main ci App token") + 1
    assert mint["with"]["permission-actions"] == "${{ github.event_name == 'merge_group' && 'write' || 'read' }}"
    step = _step()
    assert step["env"] == {
        "GH_TOKEN": "${{ steps.app-token.outputs.token }}",
        "QUEUE_REF": "${{ github.event.merge_group.head_ref }}",
        "RUN": "${{ github.run_id }}",
    }
    assert job["permissions"] == {"contents": "read"}


def test_older_runs_of_the_same_pull_request_and_older_runs_of_gone_branches_are_cancelled(tmp_path):
    result, cancelled = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert cancelled == [410, 430, 480, 455, 460, 470]
    assert f"Cancelled queue run 410 ({QUEUE}aaa111): superseded by run {RUN}" in result.stdout
    assert "Cancelled queue run 480 (gh-readonly-queue/dev/pr-77-abc777): its queue branch is gone" in result.stdout


def test_the_live_branches_are_read_after_every_run_listing(tmp_path):
    result, _ = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    urls = (tmp_path / "urls").read_text().split()
    assert [url.split("status=")[1].split("&")[0] for url in urls[:-1]] == list(RUNS)
    assert urls[-1] == "repos/owner/repo/git/matching-refs/heads/gh-readonly-queue/"


@pytest.mark.parametrize(("env", "live"), [({"FAIL_REFS": "1"}, LIVE), ({}, LIVE[1:])])
def test_unreadable_live_branches_limit_the_sweep_to_the_same_pull_request(tmp_path, env, live):
    result, cancelled = _run(tmp_path, live=live, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert cancelled == [410, 430, 455, 460, 470]
    assert "::warning::Cannot read the live queue branches" in result.stdout


def test_a_ref_outside_the_merge_queue_warns_and_cancels_nothing(tmp_path):
    result, cancelled = _run(tmp_path, ref="refs/heads/dev")
    assert result.returncode == 0, result.stdout + result.stderr
    assert cancelled == []
    assert "::warning::Unrecognised merge queue ref refs/heads/dev" in result.stdout


@pytest.mark.parametrize("failure", ["FAIL_LIST", "FAIL_CANCEL"])
def test_a_failed_listing_or_cancel_warns_without_failing_the_live_run(tmp_path, failure):
    result, cancelled = _run(tmp_path, **{failure: "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert cancelled == []
    assert "::warning::" in result.stdout
