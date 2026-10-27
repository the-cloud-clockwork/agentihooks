import json
import os
import subprocess
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
    assert set(_jobs()["scan"]["with"]["configs"].split()) == {"p/ci", "p/secrets", "p/python", ".semgrep-incidents"}


def test_outage_rules_fail_only_new_instances_and_test_their_fixtures():
    assert ".semgrep-incidents" in _jobs()["scan"]["with"]["configs"].split()
    assert any(_ROOT.glob(".semgrep-incidents/*.yml"))
    runs = [step.get("run") for step in _jobs()["feature-tests"]["steps"]]
    assert "semgrep --metrics off --test .semgrep-incidents" in runs
    assert ".semgrep-incidents/" in (_ROOT / ".semgrepignore").read_text().splitlines()


def test_scan_excludes_only_the_mutable_action_tag_rule():
    assert _jobs()["scan"]["with"]["exclude_rules"].split() == [
        "yaml.github-actions.security.github-actions-mutable-action-tag.github-actions-mutable-action-tag"
    ]


def test_scan_reads_the_domain_rules_folder_that_holds_rule_files():
    rules_dir = _ROOT / _jobs()["scan"]["with"]["rules_dir"]
    assert rules_dir == _ROOT / ".semgrep"
    assert any(_ROOT.glob(".semgrep/*.yml"))


def test_scan_and_charts_run_on_the_select_runner_expression():
    jobs = _jobs()
    select = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["select"]
    assert jobs["scan"]["with"]["runs_on"] == jobs["charts"]["runs-on"] == select["runs-on"]


def test_charts_run_the_chart_timer_guard():
    steps = _jobs()["charts"]["steps"]
    assert steps[-1]["run"] == "python tests/ci_charts/chart_timers.py"
    assert (_ROOT / "tests/ci_charts/chart_timers.py").is_file()


def test_charts_grade_the_guard_rules_against_the_base_floors():
    steps = _jobs()["charts"]["steps"]
    install = next(step for step in steps if step.get("name") == "Install PyYAML and Semgrep")
    guard = next(
        step for step in steps if step.get("name") == "Guard rules hold on their fixtures and at their computed floors"
    )
    assert install["env"] == {"SEMGREP_VERSION": "1.177.0"}
    assert guard["env"] == {"BASE": "${{ inputs.base }}"}
    assert guard["run"].splitlines() == [
        'git fetch --no-tags --depth=1 origin "${BASE#origin/}"',
        "grader=scripts/ci_semgrep_floors.py",
        "if git cat-file -e FETCH_HEAD:scripts/ci_semgrep_floors.py; then",
        '  grader="$RUNNER_TEMP/ci_semgrep_floors.py"',
        '  git show FETCH_HEAD:scripts/ci_semgrep_floors.py > "$grader"',
        "fi",
        'python "$grader" check --base FETCH_HEAD',
    ]
    assert steps.index(install) < steps.index(guard) < len(steps) - 1


SEMGREP_STUB = '#!/usr/bin/env bash\n[[ -f "$STUBS/version" ]] || exit 127\ncat "$STUBS/version"\n'
PYTHON_STUB = (
    '#!/usr/bin/env bash\necho "$*" >> "$STUBS/calls"\n'
    'if [[ "$*" == *semgrep==* ]]; then echo "$PIP_GIVES" > "$STUBS/version"; fi\n'
)


def _run_install(tmp_path, installed, pip_gives="1.177.0"):
    install = next(step for step in _jobs()["charts"]["steps"] if step.get("name") == "Install PyYAML and Semgrep")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").write_text(PYTHON_STUB)
    (bin_dir / "semgrep").write_text(SEMGREP_STUB)
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    if installed:
        (tmp_path / "version").write_text(installed + "\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUBS": str(tmp_path), "PIP_GIVES": pip_gives}
    env.update(install["env"])
    done = subprocess.run(["bash", "-euo", "pipefail", "-c", install["run"]], env=env, capture_output=True, text=True)
    calls = tmp_path / "calls"
    return done.returncode, calls.read_text().splitlines() if calls.exists() else []


def test_charts_use_the_runner_semgrep_when_it_matches_the_pin(tmp_path):
    assert _run_install(tmp_path, "1.177.0") == (0, ["-m pip install pyyaml==6.0.2"])


@pytest.mark.parametrize("installed", ["", "1.176.0"])
def test_charts_install_the_pinned_semgrep_without_a_matching_runner_copy(tmp_path, installed):
    assert _run_install(tmp_path, installed) == (0, ["-m pip install pyyaml==6.0.2 semgrep==1.177.0"])


def test_charts_fail_when_the_install_leaves_another_semgrep(tmp_path):
    assert _run_install(tmp_path, "", pip_gives="1.176.0") == (1, ["-m pip install pyyaml==6.0.2 semgrep==1.177.0"])


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
