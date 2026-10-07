import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _workflow(name):
    return yaml.safe_load((_ROOT / ".github/workflows" / name).read_text())


def test_tests_upload_durations_without_pushing_to_dev():
    workflow = _workflow("test.yml")
    assert "refresh-durations" not in workflow["jobs"]
    assert all("git push" not in step.get("run", "") for job in workflow["jobs"].values() for step in job["steps"])
    assert any(step.get("name") == "Upload durations" for step in workflow["jobs"]["unit"]["steps"])


def test_duration_refresh_batches_ci_samples_once_daily():
    workflow = _workflow("refresh-durations.yml")
    assert len(workflow[True]["schedule"]) == 1
    assert workflow["concurrency"]["cancel-in-progress"] is False
    job = workflow["jobs"]["refresh"]
    assert job["env"]["GH_TOKEN"] == "${{ secrets.ORG_GITHUB_TOKEN }}"
    command = next(step["run"] for step in job["steps"] if "run" in step)
    assert "python -m tests.refresh_durations --ci 5" in command
    assert "automation/durations-$(date -u +%F)" in command
    assert "scripts/ci_bot_pr.sh" in command


def test_release_bump_uses_a_pull_request_before_tagging():
    workflow = _workflow("release.yml")
    assert workflow[True]["workflow_dispatch"]["inputs"]["dry_run"]["default"] is False
    job = workflow["jobs"]["release"]
    steps = job["steps"]
    bump = next(i for i, step in enumerate(steps) if step.get("id") == "bump")
    assert "scripts/ci_bot_pr.sh" in steps[bump]["run"]
    assert "pyproject.toml" in steps[bump]["run"]
    tag = next(i for i, step in enumerate(steps) if step.get("name") == "Tag the merged version")
    assert bump < tag
    assert steps[tag]["env"]["MERGED"] == "${{ steps.bump.outputs.merged }}"
    assert steps[tag]["if"] == "${{ !inputs.dry_run }}"
    for step in steps[tag:]:
        assert "!inputs.dry_run" in step.get("if", "")
    assert all("git push origin dev" not in step.get("run", "") for step in steps)
    assert job["env"]["GH_TOKEN"] == "${{ secrets.ORG_GITHUB_TOKEN }}"


def _checks():
    names = ["lint", "mutation"] + [
        f"unit ({version}, {shard})" for version in ["3.11", "3.12"] for shard in range(1, 5)
    ]
    return [dict(__typename="CheckRun", name=name, status="COMPLETED", conclusion="SUCCESS") for name in names]


def _snapshot(checks=None, head="tested", state="OPEN", merge_state="CLEAN"):
    return dict(
        state=state,
        headRefOid=head,
        mergeStateStatus=merge_state,
        statusCheckRollup=_checks() if checks is None else checks,
        mergeCommit=dict(oid="merged"),
    )


def _run(tmp_path, snapshots, *, dry=False, existing=False, unchanged=False, token=True, reject_merge=False):
    tools = tmp_path / "bin"
    tools.mkdir()
    (tmp_path / "snapshots.json").write_text(json.dumps(snapshots))
    gh = tools / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + """import json
import os
import sys
from pathlib import Path
args = sys.argv[1:]
with Path('calls').open('a') as log:
    log.write('gh ' + ' '.join(args) + '\\n')
if args[:2] == ['pr', 'list']:
    print(os.environ.get('EXISTING_PR', ''))
elif args[:2] == ['pr', 'create']:
    print('https://github.com/owner/repo/pull/7')
elif args[:2] == ['pr', 'view']:
    data = json.loads(Path('snapshots.json').read_text())
    print(json.dumps(data[0]))
    if len(data) > 1:
        Path('snapshots.json').write_text(json.dumps(data[1:]))
elif args[:2] == ['pr', 'merge'] and os.environ.get('REJECT_MERGE') == '1':
    sys.exit(1)
"""
    )
    gh.chmod(0o755)
    git = tools / "git"
    git.write_text("""#!/usr/bin/env bash
set -euo pipefail
printf 'git %s\\n' "$*" >> calls
if [[ "$1 $2" == 'diff --cached' ]]; then exit "${DIFF_EXIT:-1}"; fi
""")
    git.chmod(0o755)
    sleep = tools / "sleep"
    sleep.write_text('#!/usr/bin/env bash\nset -euo pipefail\nprintf "wait\\n" >> calls\n')
    sleep.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "42",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "DIFF_EXIT": "0" if unchanged else "1",
        "EXISTING_PR": "https://github.com/owner/repo/pull/7" if existing else "",
        "REJECT_MERGE": "1" if reject_merge else "0",
    }
    env.pop("GH_TOKEN", None)
    if token:
        env["GH_TOKEN"] = "fixture"
    result = subprocess.run(
        [
            "bash",
            str(_ROOT / "scripts/ci_bot_pr.sh"),
            "automation/durations-day",
            "Refresh durations",
            ".test_durations",
            str(dry).lower(),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    log = (tmp_path / "calls").read_text() if (tmp_path / "calls").exists() else ""
    return result, log


def test_bot_update_opens_then_merges_at_the_successful_head(tmp_path):
    result, log = _run(tmp_path, [_snapshot(), _snapshot(state="MERGED")])
    assert result.returncode == 0, result.stderr
    assert "git push origin HEAD:refs/heads/automation/durations-day" in log
    assert "gh pr create --repo owner/repo --base dev --head automation/durations-day" in log
    assert (
        "gh pr merge https://github.com/owner/repo/pull/7 --repo owner/repo --squash --match-head-commit tested" in log
    )
    assert "merged=merged" in (tmp_path / "output").read_text()
    assert "HEAD:dev" not in log


@pytest.mark.parametrize("mode", ["missing", "pending", "empty"])
def test_bot_update_waits_for_all_tests_before_merging(tmp_path, mode):
    checks = _checks()
    if mode == "missing":
        checks.pop()
    elif mode == "pending":
        checks[0]["status"] = "IN_PROGRESS"
        checks[0]["conclusion"] = None
    else:
        checks = []
    result, log = _run(tmp_path, [_snapshot(checks), _snapshot(), _snapshot(state="MERGED")])
    assert result.returncode == 0, result.stderr
    assert log.index("wait") < log.index("gh pr merge")
    assert log.count("gh pr merge") == 1


@pytest.mark.parametrize("conclusion", ["FAILURE", "CANCELLED", "SKIPPED", "TIMED_OUT"])
def test_bot_update_refuses_unsuccessful_checks(tmp_path, conclusion):
    checks = _checks()
    checks[0]["conclusion"] = conclusion
    result, log = _run(tmp_path, [_snapshot(checks)])
    assert result.returncode != 0
    assert "gh pr merge" not in log


def test_dry_update_opens_its_pull_request_without_merging(tmp_path):
    result, log = _run(tmp_path, [], dry=True)
    assert result.returncode == 0, result.stderr
    assert "gh pr create" in log
    assert "gh pr view" not in log
    assert "gh pr merge" not in log


def test_daily_refresh_reuses_its_existing_pull_request(tmp_path):
    result, log = _run(tmp_path, [_snapshot(), _snapshot(state="MERGED")], existing=True)
    assert result.returncode == 0, result.stderr
    assert "git push" not in log
    assert "gh pr create" not in log
    assert "gh pr merge" in log


def test_unchanged_durations_do_not_create_a_pull_request(tmp_path):
    result, log = _run(tmp_path, [], unchanged=True)
    assert result.returncode == 0, result.stderr
    assert "git push" not in log
    assert "gh pr create" not in log


def test_update_without_an_automation_token_fails_before_push(tmp_path):
    result, log = _run(tmp_path, [], token=False)
    assert result.returncode != 0
    assert log == ""


def test_changed_head_is_refused_by_the_pinned_merge(tmp_path):
    result, log = _run(tmp_path, [_snapshot()], reject_merge=True)
    assert result.returncode != 0
    assert "--match-head-commit tested" in log
    assert log.count("gh pr merge") == 1


def test_bot_update_refreshes_a_branch_that_fell_behind_dev(tmp_path):
    result, log = _run(
        tmp_path, [_snapshot(merge_state="BEHIND"), _snapshot(head="updated"), _snapshot(state="MERGED")]
    )
    assert result.returncode == 0, result.stderr
    assert "pulls/7/update-branch -f expected_head_sha=tested" in log
    assert "--match-head-commit updated" in log
    assert "--match-head-commit tested" not in log


@pytest.mark.parametrize("bump,expected", [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")])
def test_release_computes_the_requested_version(tmp_path, bump, expected):
    step = next(step for step in _workflow("release.yml")["jobs"]["release"]["steps"] if step.get("id") == "version")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "BUMP": bump, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert f"next={expected}\n" in output.read_text()
