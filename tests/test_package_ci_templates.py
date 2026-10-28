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
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == "promotion-dev-${{ github.run_id }}"
    assert concurrency["cancel-in-progress"] is False
    promote = workflow["jobs"]["promote"]
    assert promote["needs"] == "checks"
    assert "if" not in promote
    assert promote["concurrency"] == {"group": "release-version", "cancel-in-progress": False}
    assert "concurrency" not in workflow["jobs"]["checks"]


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
