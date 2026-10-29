from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/test.yml"


def test_documentation_pull_requests_build_the_pages_site_without_waiting_for_lint():
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    pages = yaml.safe_load((_WORKFLOW.parent / "pages.yml").read_text())
    assert workflow[True]["pull_request"]["branches"] == ["dev"]
    build = workflow["jobs"]["docs"]
    assert "lint" not in build["needs"]
    assert "docs" in workflow["jobs"]["gate-required"]["needs"]
    assert "docs" in workflow["jobs"]["stage-budget"]["needs"]
    jekyll = [step for step in build["steps"] if step.get("name") == "Build docs with Jekyll"]
    release = [step for step in pages["jobs"]["build"]["steps"] if step.get("uses") == "actions/jekyll-build-pages@v1"]
    assert len(jekyll) == len(release) == 1
    inputs = jekyll[0].get("with") or {
        "source": jekyll[0]["env"]["INPUT_SOURCE"],
        "destination": jekyll[0]["env"]["INPUT_DESTINATION"],
    }
    assert inputs == release[0]["with"] == {"source": "./", "destination": "./_site"}
    assert not any(step.get("name") == "Build docs with Jekyll" for step in workflow["jobs"]["lint"]["steps"])
    assert "github.event_name != 'merge_group'" in build["if"]
    assert "github.event_name != 'push'" not in build["if"]


def test_documentation_validation_cannot_configure_upload_or_deploy_pages():
    build = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["docs"]
    assert build["permissions"] == {"contents": "read"}
    assert "environment" not in build
    actions = [step["uses"] for step in build["steps"] if "uses" in step]
    assert actions in (
        ["actions/checkout@v4", "actions/jekyll-build-pages@v1", "./.github/actions/retry-upload"],
        ["actions/checkout@v4", "./.github/actions/retry-upload"],
    )
    assert build["steps"][0]["with"]["persist-credentials"] is False
    artifact = next(step for step in build["steps"] if step.get("uses") == "./.github/actions/retry-upload")
    assert artifact["with"] == {
        "name": "documentation-site",
        "path": "_site/",
        "retention-days": 7,
        "if-no-files-found": "error",
    }
    assert "secrets." not in yaml.safe_dump(build)
