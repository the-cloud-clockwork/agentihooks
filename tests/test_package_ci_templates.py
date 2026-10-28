from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "profiles/package/templates/ci"


@pytest.mark.parametrize(
    ("filename", "event", "tier"),
    [
        ("pull-request.yml", "pull_request", "pull_request"),
        ("merge-queue.yml", "merge_group", "queue"),
        ("promotion.yml", "push", "promotion"),
    ],
)
def test_workflow_template_contract(filename, event, tier):
    workflow = yaml.safe_load((TEMPLATES / filename).read_text())
    assert event in workflow[True]
    job = workflow["jobs"]["checks"]
    assert job["runs-on"] == "${{ vars.CI_RUNNER_LABEL || 'ubuntu-latest' }}"
    assert job["env"]["DELIVERY_TIER"] == tier
    assert workflow["permissions"] == {"contents": "read"}
    assert any("delivery.yaml" in step.get("run", "") for step in job["steps"])
    if tier == "promotion":
        assert workflow[True]["push"]["branches"] == ["dev"]
        assert workflow["concurrency"]["cancel-in-progress"] is False
        promote = workflow["jobs"]["promote"]
        assert promote["needs"] == "checks"
        assert promote["environment"] == "promotion"
    else:
        assert job["name"] == "Delivery required"
        assert "promote" not in workflow["jobs"]


def test_promotion_template_does_not_hold_green_push_behind_red_run():
    workflow = yaml.safe_load((TEMPLATES / "promotion.yml").read_text())
    assert workflow["concurrency"]["group"] == "promotion-dev-${{ github.run_id }}"
    promote = workflow["jobs"]["promote"]
    assert "if" not in promote
    assert promote["concurrency"] == {"group": "release-version", "cancel-in-progress": False}
    assert "concurrency" not in workflow["jobs"]["checks"]


@pytest.mark.parametrize(
    ("order", "allowed"),
    [("behind", False), ("identical", False), ("ahead", True), ("diverged", False)],
)
def test_promotion_template_refuses_superseded_head(tmp_path, monkeypatch, order, allowed):
    import os
    import subprocess
    import sys

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "--initial-branch=dev")
    git("config", "user.name", "Promotion fixture")
    git("config", "user.email", "promotion@example.invalid")
    git("commit", "--allow-empty", "-m", "Initial")
    initial = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "Older graded head")
    older = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "Already promoted head")
    promoted = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "Newer graded head")
    newer = git("rev-parse", "HEAD")
    git("checkout", "-b", "main", initial)
    git("commit", "--allow-empty", "-m", f"Release\n\nGraded dev revision: {promoted}")
    main = git("rev-parse", "HEAD")
    git("remote", "add", "origin", str(repo))
    git("checkout", "--detach", initial)
    git("commit", "--allow-empty", "-m", "Unrelated head")
    diverged = git("rev-parse", "HEAD")
    graded = {"behind": older, "identical": promoted, "ahead": newer, "diverged": diverged}[order]
    (repo / "delivery.yaml").write_text("promote:\n  - touch adopter-ran\n")
    monkeypatch.setenv("GITHUB_SHA", graded)
    workflow = yaml.safe_load((TEMPLATES / "promotion.yml").read_text())
    command = next(
        step["run"]
        for step in workflow["jobs"]["promote"]["steps"]
        if step.get("name") == "Promote the graded revision"
    ).replace("python -", f"{sys.executable} -")
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", command],
        cwd=repo,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is allowed, result.stdout + result.stderr
    assert (repo / "adopter-ran").exists() is allowed
    assert git("rev-parse", "main") == main
    if not allowed:
        assert "not newer than the released dev revision" in result.stderr


def test_delivery_example_and_adoption_instructions():
    delivery = yaml.safe_load((TEMPLATES / "delivery.yaml").read_text())
    assert set(delivery) == {"pull_request", "queue", "promotion", "promote"}
    assert delivery["queue"] == ["./ci/unit"]
    assert delivery["promote"] == ['./ci/promote "$GITHUB_SHA"']
    readme = (TEMPLATES / "README.md").read_text()
    for required in ("CI_RUNNER_LABEL", ".github/workflows", "delivery.yaml", "fixer", "retry", "promotion"):
        assert required in readme


def test_ci_actionlint_grades_the_packaged_templates():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci-templates.yml").read_text())
    steps = workflow["jobs"]["actionlint"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "profiles/package/templates/ci/*.yml" in commands
    assert "invalid.yml" in commands
    assert "if actionlint" in commands
