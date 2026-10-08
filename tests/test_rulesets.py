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
    assert rules["pull_request"]["parameters"]["require_code_owner_review"] is False


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
