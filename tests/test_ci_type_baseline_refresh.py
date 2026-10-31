from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/type-baseline-refresh.yml"


def _workflow() -> dict:
    """Return the parsed refresh workflow."""
    return yaml.safe_load(_WORKFLOW.read_text())


def _steps(job: str) -> list[dict]:
    """Return one job's steps."""
    return _workflow()["jobs"][job]["steps"]


def _run(job: str) -> str:
    """Return every shell line a job runs."""
    return "\n".join(step.get("run", "") for step in _steps(job))


def test_the_refresh_runs_daily_and_on_dispatch() -> None:
    triggers = _workflow()[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert [entry["cron"].split()[2:] for entry in triggers["schedule"]] == [["*", "*", "*"]]


def test_the_refresh_measures_dev_with_the_type_gate() -> None:
    assert _steps("refresh")[0]["with"]["ref"] == "dev"
    assert 'python -m scripts.type_gate --refresh --head "$GITHUB_WORKSPACE"' in _run("refresh")


def test_only_a_lowered_baseline_opens_one_pull_request_into_dev() -> None:
    run = _run("refresh")
    assert "git diff --quiet -- tests/TYPE_BASELINE.json" in run
    assert 'path: "tests/TYPE_BASELINE.json"' in run
    assert 'git/commits" -f message="Lower the type baseline to the measured counts"' in run
    assert "git push" not in run and "x-access-token" not in run
    assert run.index('"$(git hash-object tests/TYPE_BASELINE.json)"') < run.index("git/blobs")
    assert '--head "$BRANCH" --base dev --state open' in run
    assert 'gh pr create --repo "$GITHUB_REPOSITORY" --base dev --head "$BRANCH"' in run
    pull = next(step for step in _steps("refresh") if step.get("id") == "pull")
    assert pull["if"] == "steps.diff.outputs.changed == 'true'"


def test_the_promotion_app_token_pushes_opens_and_enqueues() -> None:
    for job in ("refresh", "enqueue"):
        token = next(step for step in _steps(job) if step.get("id") == "app-token")
        assert token["uses"].startswith("actions/create-github-app-token@")
        assert token["with"]["client-id"] == "${{ secrets.TCC_CI_CLIENT_ID }}"
        assert token["with"]["permission-pull-requests"] == "write"
    assert _workflow()["jobs"]["refresh"]["permissions"] == {"contents": "read"}
    assert _workflow()["jobs"]["enqueue"]["permissions"] == {"contents": "read"}


def test_the_pull_request_enqueues_only_after_its_required_gate_succeeds() -> None:
    run = _run("enqueue")
    assert "check_name=Gate%20%E2%80%94%20Required" in run
    assert "completed:success) break" in run
    assert "completed:*)" in run and "exit 1" in run
    assert run.index("completed:success") < run.index("enqueuePullRequest")
    assert "expectedHeadOid: $head" in run
    assert run.index("mergeQueueEntry != null") < run.index("enqueuePullRequest")
    assert "gh pr merge" not in run
