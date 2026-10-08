import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["dev-no-delete", "main-prod-lockdown"])
def test_rulesets_enforce_branch_protection_without_bypass(name):
    ruleset = json.loads((ROOT / ".github" / "rulesets" / f"{name}.json").read_text())

    assert ruleset["name"] == name
    assert ruleset["target"] == "branch"
    assert ruleset["enforcement"] == "active"
    assert ruleset["bypass_actors"] == []
    rules = {rule["type"]: rule for rule in ruleset["rules"]}
    assert {"deletion", "non_fast_forward", "required_linear_history", "pull_request"} <= rules.keys()
    assert rules["pull_request"]["parameters"]["required_approving_review_count"] == 0
    assert rules["pull_request"]["parameters"]["require_code_owner_review"] is (name == "dev-no-delete")


def test_dev_merges_through_a_queue_that_runs_the_workflow_gate():
    ruleset = json.loads((ROOT / ".github" / "rulesets" / "dev-no-delete.json").read_text())
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "test.yml").read_text())

    assert ruleset["conditions"] == {"ref_name": {"include": ["refs/heads/dev"], "exclude": []}}
    rules = {rule["type"]: rule for rule in ruleset["rules"]}
    params = rules["required_status_checks"]["parameters"]
    assert params["strict_required_status_checks_policy"] is False
    assert params["do_not_enforce_on_create"] is False
    checks = params["required_status_checks"]
    assert len(checks) == 1
    assert checks[0]["context"] == workflow["jobs"]["gate-required"]["name"] == "Gate — Required"
    assert rules["merge_queue"]["parameters"] == {
        "check_response_timeout_minutes": 60,
        "grouping_strategy": "ALLGREEN",
        "max_entries_to_build": 5,
        "max_entries_to_merge": 5,
        "merge_method": "SQUASH",
        "min_entries_to_merge": 1,
        "min_entries_to_merge_wait_minutes": 0,
    }
    assert rules["pull_request"]["parameters"]["allowed_merge_methods"] == ["squash"]
    assert workflow[True]["merge_group"] == {"types": ["checks_requested"]}


def test_main_keeps_its_existing_branch_match_and_rules():
    ruleset = json.loads((ROOT / ".github" / "rulesets" / "main-prod-lockdown.json").read_text())

    assert ruleset["conditions"] == {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}
    rules = {rule["type"]: rule for rule in ruleset["rules"]}
    assert set(rules) == {"deletion", "non_fast_forward", "required_linear_history", "pull_request"}
    assert rules["pull_request"]["parameters"] == {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": False,
        "required_reviewers": [],
        "require_code_owner_review": False,
        "dismissal_restriction": {"enabled": False, "allowed_actors": []},
        "require_last_push_approval": False,
        "required_review_thread_resolution": False,
        "require_extra_approval_for_unattributed_changes": True,
        "allowed_merge_methods": ["merge", "squash", "rebase"],
    }


def _environment(name):
    return json.loads((ROOT / ".github" / "environments" / f"{name}.json").read_text())


def _workflow_environments(name):
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
    return {job["environment"] for job in workflow["jobs"].values() if "environment" in job}


@pytest.mark.parametrize(
    ("name", "workflow", "policies"),
    [
        ("release", "release.yml", [{"name": "dev", "type": "branch"}]),
        ("pypi", "publish-pypi.yml", [{"name": "main", "type": "branch"}, {"name": "v*", "type": "tag"}]),
    ],
)
def test_environment_deploys_only_from_its_release_dance_refs(name, workflow, policies):
    environment = _environment(name)

    assert _workflow_environments(workflow) == {name}
    assert environment["environment"]["deployment_branch_policy"] == {
        "protected_branches": False,
        "custom_branch_policies": True,
    }
    assert environment["deployment_branch_policies"] == policies


def test_publish_keeps_the_operator_reviewer():
    reviewers = _environment("pypi")["environment"]["reviewers"]

    assert reviewers == [{"type": "User", "id": 31187725}]


def test_version_tags_cannot_be_moved_or_deleted():
    ruleset = json.loads((ROOT / ".github" / "rulesets" / "version-tags.json").read_text())

    assert ruleset["name"] == "version-tags"
    assert ruleset["target"] == "tag"
    assert ruleset["enforcement"] == "active"
    assert ruleset["conditions"] == {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}}
    assert ruleset["bypass_actors"] == []
    assert {rule["type"] for rule in ruleset["rules"]} == {"update", "deletion", "non_fast_forward"}
