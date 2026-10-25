import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
SHARED = "The-Cloud-Clockwork/.github/.github/workflows/semgrep-reusable.yml@main"


def _semgrep():
    return yaml.safe_load((_ROOT / ".github/workflows/semgrep.yml").read_text())


def _jobs():
    return _semgrep()["jobs"]


def test_wrapper_runs_the_shared_scan_feature_tests_and_chart_guard():
    assert set(_jobs()) == {"scan", "feature-tests", "charts"}


def test_scan_calls_the_shared_workflow_on_its_main_branch():
    assert _jobs()["scan"]["uses"] == SHARED


def test_scan_passes_exactly_the_shared_workflow_inputs():
    assert set(_jobs()["scan"]["with"]) == {"base", "configs", "exclude_rules", "rules_dir", "runs_on"}


def test_scan_grades_new_findings_against_the_caller_base():
    document = _semgrep()
    base = document[True]["workflow_call"]["inputs"]["base"]
    assert base["type"] == "string"
    assert base["required"] is True
    assert _jobs()["scan"]["with"]["base"] == "${{ inputs.base }}"


def test_scan_runs_the_registry_packs_for_ci_secrets_and_python():
    assert set(_jobs()["scan"]["with"]["configs"].split()) == {"p/ci", "p/secrets", "p/python"}


def test_scan_excludes_only_the_mutable_action_tag_rule():
    assert _jobs()["scan"]["with"]["exclude_rules"].split() == [
        "yaml.github-actions.security.github-actions-mutable-action-tag.github-actions-mutable-action-tag"
    ]


def test_scan_reads_the_domain_rules_folder_that_holds_rule_files():
    rules_dir = _ROOT / _jobs()["scan"]["with"]["rules_dir"]
    assert rules_dir == _ROOT / ".semgrep"
    assert any(rules_dir.glob("*.yml"))


def test_scan_and_charts_run_on_the_select_runner_expression():
    jobs = _jobs()
    select = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["select"]
    assert jobs["scan"]["with"]["runs_on"] == jobs["charts"]["runs-on"] == select["runs-on"]


def test_charts_run_the_chart_timer_guard():
    steps = _jobs()["charts"]["steps"]
    assert steps[-1]["run"] == "python tests/ci_charts/chart_timers.py"
    assert (_ROOT / "tests/ci_charts/chart_timers.py").is_file()


def test_no_semgrep_job_or_step_can_pass_on_a_skip_or_an_error():
    jobs = _jobs()
    assert not {"if", "continue-on-error"} & (set(jobs["scan"]) | set(jobs["charts"]))
    assert all("if" not in step and "continue-on-error" not in step for step in jobs["charts"]["steps"])


def test_wrapper_holds_only_read_permission():
    assert _semgrep()["permissions"] == {"contents": "read"}


def test_feature_tests_and_charts_have_runner_classes():
    classes = json.loads((_ROOT / ".github/runner-classes.json").read_text())
    assert {key for key in classes if key.startswith("semgrep.yml/")} == {
        "semgrep.yml/charts",
        "semgrep.yml/feature-tests",
    }
