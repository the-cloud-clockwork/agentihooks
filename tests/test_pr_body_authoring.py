from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "profiles/package/roles/engineer/.claude/skills/swarm-engineer/SKILL.md"


@pytest.mark.feature("pull-request-recon-context")
def test_existing_template_authoring_sections_remain() -> None:
    text = (ROOT / ".github/pull_request_template.md").read_text()
    for heading in ("Summary", "Changes", "Test Plan", "Checklist"):
        assert f"## {heading}\n" in text
    assert "Changelog fragment added" in text
    assert "hooks_list_tools" in text


@pytest.mark.feature("pull-request-recon-context")
def test_engineer_preserves_first_push_and_ci_requirements() -> None:
    text = SKILL.read_text()
    assert text.index("## Plan") < text.index("## Work")
    assert "draft pull request" in text
    assert "at that first push" in text
    assert "red in CI" in text
    assert "green" in text
    assert "AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=true" in text


@pytest.mark.feature("pull-request-recon-context")
def test_engineer_fills_body_from_recon() -> None:
    text = SKILL.read_text()
    assert "python -m scripts.pr_body fill" in text
    assert "--recon" in text
    assert "--body" in text
    assert "level three" in text
    assert "Notes for the reviewer" in text


@pytest.mark.feature("pull-request-recon-context")
def test_protected_body_check_runs_on_body_edits() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/gate-protected.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    assert set(events["pull_request_target"]["types"]) == {"opened", "synchronize", "reopened", "edited"}
    steps = workflow["jobs"]["grade"]["steps"]
    body_step = next(step for step in steps if step.get("id") == "body")
    assert body_step["run"] == 'python -m scripts.pr_body check --event "$GITHUB_EVENT_PATH"'
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.base_ref }}"
    assert steps.index(body_step) < next(i for i, step in enumerate(steps) if step.get("id") == "grade")
    assert "github.event.pull_request.body" not in str(workflow)
