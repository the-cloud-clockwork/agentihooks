import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_budget
from tests import flake_budget

DAY = 24 * 3600
NOW = 1_791_700_000.0
TEST = "tests/test_page.py::test_the_fold_survives_a_reload"
UNIT = "test-v1-" + "a" * 64
SETTINGS = ("CI_LAB_MINIO_ENDPOINT", "CI_LAB_MINIO_BUCKET", "CI_LAB_MINIO_ACCESS_KEY", "CI_LAB_MINIO_SECRET_KEY")
FAKE_PYTHON = """#!/usr/bin/env bash
echo "python $*" >> "$CALLS"
if [[ "$1 $2" == "-m venv" ]]; then
  [[ "$FAIL" != venv ]] || exit 1
  mkdir -p "$3/bin"
  exit 0
fi
[[ "$FAIL" != pip ]]
"""
FAKE_AGENTIHOOKS = """#!/usr/bin/env bash
echo "agentihooks $*" >> "$CALLS"
if [[ "$FAIL" == agentihooks ]]; then
  echo "ci lab flakes: Could not connect to the endpoint URL" >&2
  exit 1
fi
mkdir -p "$5"
printf '{}\\n{}\\n' > "$5/lab-flakes.jsonl"
"""


def _report(test=TEST, run="7", outcome="flaky"):
    return {"test": test, "run": run, "run_attempt": "1", "attempts": 2, "outcome": outcome, "errors": ["E boom"]}


@pytest.mark.feature("flake-budget-once-per-run")
def test_a_flaky_report_joins_the_history_once_per_run():
    history = flake_budget.gather([], [_report(), _report(f"{TEST}[250]"), _report(run=8)], NOW)
    assert history == [{"test": TEST, "run": "7", "at": NOW}, {"test": TEST, "run": "8", "at": NOW}]
    assert flake_budget.gather(history, [_report()], NOW + 60) == history


@pytest.mark.feature("flake-budget-once-per-run")
def test_retry_evidence_survives_history_deduplication_and_expiry():
    first = {**_report(f"{TEST}[first]"), "tracebacks": ["first frame\nRuntimeError: first"]}
    second = {**_report(f"{TEST}[second]"), "tracebacks": ["second frame\nRuntimeError: second"]}
    third = {**_report(run="8"), "tracebacks": ["third frame\nRuntimeError: third"]}
    history = flake_budget.gather([], [first, second, first], NOW)
    history = flake_budget.gather(history, [first, third], NOW + 60)
    assert flake_budget.budget(history)["retried"] == [
        {"test": TEST, "count": 2, "runs": ["7", "8"], "reports": [first, second, third]}
    ]
    history = flake_budget.gather(history, [], NOW + flake_budget.WINDOW_S)
    assert flake_budget.budget(history)["retried"] == [{"test": TEST, "count": 1, "runs": ["8"], "reports": [third]}]


def test_a_failed_report_is_not_a_flake():
    assert flake_budget.gather([], [_report(outcome="failed")], NOW) == []


@pytest.mark.feature("flake-budget-seven-day-expiry")
def test_lines_older_than_seven_days_drop_out():
    edge = {"test": TEST, "run": "1", "at": NOW - 7 * DAY}
    inside = {"test": TEST, "run": "2", "at": NOW - 7 * DAY + 1}
    assert flake_budget.gather([edge, inside], [], NOW) == [inside]
    assert flake_budget.gather([edge], [_report(run="1")], NOW) == [{"test": TEST, "run": "1", "at": NOW}]


@pytest.mark.feature("flake-budget-over-at-two-runs")
def test_a_test_flaky_in_two_runs_is_over_budget():
    other = "tests/test_x.py::test_once"
    history = [
        {"test": TEST, "run": "9", "at": NOW},
        {"test": other, "run": "9", "at": NOW},
        {"test": TEST, "run": "10", "at": NOW},
    ]
    assert flake_budget.budget(history)["over_budget"] == [{"test": TEST, "runs": ["10", "9"]}]
    assert flake_budget.budget(history[:2])["over_budget"] == []
    assert flake_budget.budget([]) == {"flaky_tests": 0, "over_budget": [], "retried": []}


@pytest.mark.feature("flake-budget-over-at-two-runs")
def test_every_retry_pass_is_listed_with_its_count_and_runs():
    other = "tests/test_x.py::test_once"
    history = [
        {"test": TEST, "run": "9", "at": NOW},
        {"test": other, "run": "9", "at": NOW},
        {"test": TEST, "run": "10", "at": NOW},
    ]
    assert flake_budget.budget(history) == {
        "flaky_tests": 2,
        "over_budget": [{"test": TEST, "runs": ["10", "9"]}],
        "retried": [
            {"test": TEST, "count": 2, "runs": ["10", "9"]},
            {"test": other, "count": 1, "runs": ["9"]},
        ],
    }
    assert flake_budget.budget(history[1:2]) == {
        "flaky_tests": 1,
        "over_budget": [],
        "retried": [{"test": other, "count": 1, "runs": ["9"]}],
    }


def test_the_command_keeps_the_history_and_writes_the_budget(tmp_path, capsys):
    reports = tmp_path / "reports"
    (reports / "flakes-3.12-1").mkdir(parents=True)
    (reports / "flakes-3.12-1" / "flakes.jsonl").write_text(json.dumps(_report(run="20")) + "\n\n")
    (reports / "flakes-3.11-2").mkdir()
    (reports / "flakes-3.11-2" / "flakes.jsonl").write_text("")
    history = tmp_path / "cache" / "history.jsonl"
    out = tmp_path / "flake-budget.json"
    argv = ["--history", str(history), "--reports", str(reports), "--out", str(out), "--now", str(NOW)]

    assert flake_budget.main(argv) == 0
    assert json.loads(out.read_text()) == {
        "flaky_tests": 1,
        "over_budget": [],
        "retried": [{"test": TEST, "count": 1, "runs": ["20"]}],
    }
    assert capsys.readouterr().out.splitlines()[-1] == f"{TEST} passed on a retry in 1 dev runs: 20 (actions)"
    (reports / "flakes-3.12-1" / "flakes.jsonl").write_text(json.dumps(_report(run="21")) + "\n")
    assert flake_budget.main(argv[:-1] + [str(NOW + 60)]) == 0

    assert [json.loads(line)["run"] for line in history.read_text().splitlines()] == ["20", "21"]
    assert json.loads(out.read_text()) == {
        "flaky_tests": 1,
        "over_budget": [{"test": TEST, "runs": ["20", "21"]}],
        "retried": [{"test": TEST, "count": 2, "runs": ["20", "21"]}],
    }
    printed = capsys.readouterr().out.splitlines()
    assert printed[-3:] == [
        "Flaky test ids in the last seven days of dev runs: 1",
        f"{TEST} passed on a retry in 2 dev runs: 20 (actions), 21 (actions)",
        f"::warning title=Flake budget::{TEST} was flaky in 2 dev runs: 20 (actions), 21 (actions)",
    ]


def test_the_command_takes_the_clock_when_no_time_is_given(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "flakes.jsonl").write_text(json.dumps(_report()) + "\n")
    monkeypatch.setattr(flake_budget.time, "time", lambda: NOW)
    history = tmp_path / "history.jsonl"
    argv = ["--history", str(history), "--reports", str(reports), "--out", str(tmp_path / "out.json")]
    assert flake_budget.main(argv) == 0
    assert json.loads(history.read_text()) == {"test": TEST, "run": "7", "at": NOW}


@pytest.mark.feature("flake-budget-no-reports-red")
def test_no_flake_reports_is_red(tmp_path, capsys):
    out = tmp_path / "out.json"
    argv = ["--history", str(tmp_path / "h.jsonl"), "--reports", str(tmp_path), "--out", str(out)]
    assert flake_budget.main(argv) == 1
    assert capsys.readouterr().out == f"::error::No flake reports under {tmp_path}\n"
    assert not out.exists()


@pytest.mark.feature("flake-budget-job-wiring")
def test_a_dev_push_keeps_the_flake_history_and_publishes_the_budget():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())
    job = workflow["jobs"]["flake-budget"]
    assert job["needs"] == ["unit"]
    assert job["if"] == "${{ !cancelled() && github.event_name == 'push' }}"
    assert job["permissions"] == {"contents": "read"}
    assert "flake-budget" not in workflow["jobs"]["gate-required"]["needs"]
    assert ci_budget.BUDGETS["flake-budget"] == 120
    steps = {step.get("name", step.get("uses")): step for step in job["steps"]}
    key = "flake-history-${{ github.run_id }}-${{ github.run_attempt }}"
    restore = steps["Restore the flake history"]
    assert restore["uses"] == "actions/cache/restore@v4"
    assert restore["with"] == {
        "path": "~/flake-history",
        "key": key,
        "restore-keys": "flake-history-\nflake-seed-\n",
    }
    unmatched = "steps.restore-flake-history.outputs.cache-matched-key == ''"
    assert steps["Restore the flake history, wait before attempt 2"]["if"] == unmatched
    second = steps["Restore the flake history, attempt 2"]
    assert second["if"] == unmatched
    assert {name: second[name] for name in ("uses", "env", "with")} == {
        name: restore[name] for name in ("uses", "env", "with")
    }
    assert steps["Download this run's flake reports"]["with"] == {"pattern": "flakes-3.*", "path": "~/flake-reports"}
    assert steps["Count flaky tests over seven days"]["run"].startswith(
        "python -m tests.flake_budget --history ~/flake-history/history.jsonl --reports ~/flake-reports"
        " --out flake-budget.json"
    )
    assert steps["Count flaky tests over seven days"]["shell"] == "bash"
    matched = "steps.restore-flake-history.outputs.cache-matched-key || steps.restore-flake-history-2.outputs.cache-matched-key"
    assert steps["Publish the flake budget"]["with"]["name"] == "flake-budget"
    assert steps["Publish the flake budget"]["with"]["path"] == "flake-budget.json"
    assert steps["Publish the flake budget"]["with"]["if-no-files-found"] == "error"
    assert steps["Save the flake history"]["uses"] == "actions/cache/save@v4"
    assert steps["Publish the flake budget"]["if"] == matched
    assert steps["Save the flake history"]["with"] == {
        "path": "~/flake-history",
        "key": f"${{{{ ({matched}) && 'flake-history' || 'flake-seed' }}}}-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}",
    }
    order = [
        "Restore the flake history",
        "Restore the flake history, wait before attempt 2",
        "Restore the flake history, attempt 2",
        "Download this run's flake reports",
        "Export the lab flake records",
        "Count flaky tests over seven days",
        "Publish the flake budget",
        "Save the flake history",
    ]
    names = list(steps)
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)


@pytest.mark.feature("flake-budget-job-wiring")
def test_a_restore_that_matched_nothing_saves_a_seed_the_history_prefix_never_restores():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())
    steps = {step.get("name"): step for step in workflow["jobs"]["flake-budget"]["steps"]}
    prefixes = steps["Restore the flake history"]["with"]["restore-keys"].split()
    assert prefixes == ["flake-history-", "flake-seed-"]
    save = steps["Save the flake history"]["with"]["key"]
    assert "|| 'flake-seed' }}-" in save


@pytest.mark.feature("flake-budget-lab-source")
def test_a_lab_history_line_names_its_run_lab_and_a_line_without_a_source_counts_as_actions() -> None:
    old = {"test": TEST, "run": "9", "at": NOW}
    history = [old, {"test": TEST, "run": UNIT, "at": NOW, "source": "lab"}]
    sources = {"9": "actions", UNIT: "lab"}
    assert flake_budget.budget(history) == {
        "flaky_tests": 1,
        "over_budget": [{"test": TEST, "runs": ["9", UNIT], "sources": sources}],
        "retried": [{"test": TEST, "count": 2, "runs": ["9", UNIT], "sources": sources}],
    }
    assert flake_budget.budget([old]) == {
        "flaky_tests": 1,
        "over_budget": [],
        "retried": [{"test": TEST, "count": 1, "runs": ["9"]}],
    }


@pytest.mark.feature("flake-budget-lab-source")
def test_a_lab_export_alone_grades_green_and_names_its_run_lab(tmp_path: Path) -> None:
    lab = tmp_path / "reports" / "lab"
    lab.mkdir(parents=True)
    (lab / "lab-flakes.jsonl").write_text(json.dumps(_report(run=UNIT)) + "\n")
    history = tmp_path / "history.jsonl"
    out = tmp_path / "flake-budget.json"
    argv = ["--history", str(history), "--reports", str(tmp_path / "reports"), "--out", str(out), "--now", str(NOW)]

    assert flake_budget.main(argv) == 0
    assert json.loads(out.read_text()) == {
        "flaky_tests": 1,
        "over_budget": [],
        "retried": [{"test": TEST, "count": 1, "runs": [UNIT], "sources": {UNIT: "lab"}}],
    }
    assert json.loads(history.read_text()) == {"test": TEST, "run": UNIT, "at": NOW, "source": "lab"}

    (lab / "lab-flakes.jsonl").write_text("")
    history.unlink()
    assert flake_budget.main(argv) == 0
    assert json.loads(out.read_text()) == {"flaky_tests": 0, "over_budget": [], "retried": []}


@pytest.mark.feature("flake-budget-lab-source")
def test_the_budget_names_each_runs_source_in_its_json_and_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    (reports / "flakes-3.12-1").mkdir(parents=True)
    (reports / "flakes-3.12-1" / "flakes.jsonl").write_text(json.dumps(_report(run="20")) + "\n")
    (reports / "lab").mkdir()
    (reports / "lab" / "lab-flakes.jsonl").write_text(json.dumps(_report(run=UNIT)) + "\n")
    out = tmp_path / "flake-budget.json"
    argv = ["--history", str(tmp_path / "h.jsonl"), "--reports", str(reports), "--out", str(out), "--now", str(NOW)]

    assert flake_budget.main(argv) == 0
    sources = {"20": "actions", UNIT: "lab"}
    assert json.loads(out.read_text())["over_budget"] == [{"test": TEST, "runs": ["20", UNIT], "sources": sources}]
    assert capsys.readouterr().out.splitlines()[-2:] == [
        f"{TEST} passed on a retry in 2 dev runs: 20 (actions), {UNIT} (lab)",
        f"::warning title=Flake budget::{TEST} was flaky in 2 dev runs: 20 (actions), {UNIT} (lab)",
    ]


@pytest.mark.feature("flake-budget-job-wiring")
def test_the_job_exports_the_lab_flake_records_before_the_count_with_the_settings_by_secret_reference() -> None:
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())
    job = workflow["jobs"]["flake-budget"]
    names = [step.get("name") for step in job["steps"]]
    at = names.index("Export the lab flake records")
    step = job["steps"][at]
    assert names[at - 1 : at + 2] == [
        "Download this run's flake reports, attempt 2",
        "Export the lab flake records",
        "Count flaky tests over seven days",
    ]
    assert step["shell"] == "bash"
    assert "continue-on-error" not in step
    assert step["env"] == {
        "CI_LAB_MINIO_ENDPOINT": "${{ secrets.CI_LAB_FLAKES_ENDPOINT }}",
        "CI_LAB_MINIO_BUCKET": "${{ secrets.CI_LAB_FLAKES_BUCKET }}",
        "CI_LAB_MINIO_ACCESS_KEY": "${{ secrets.CI_LAB_FLAKES_ACCESS_KEY }}",
        "CI_LAB_MINIO_SECRET_KEY": "${{ secrets.CI_LAB_FLAKES_SECRET_KEY }}",
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS": "0.dev${{ github.run_number }}",
    }
    assert "true" not in step["run"].split()
    assert "timeout 120 python -m pip install --quiet --constraint .github/test-requirements.lock -e ." in step["run"]
    assert "timeout 45 agentihooks ci lab flakes --out ~/flake-reports/lab" in step["run"]
    assert job["timeout-minutes"] == 8


@pytest.mark.feature("flake-budget-job-wiring")
@pytest.mark.parametrize(
    ("unset", "fail", "warning", "made"),
    [
        ("CI_LAB_MINIO_SECRET_KEY", "", "The lab store settings are not set", 0),
        ("", "venv", "Creating the lab export environment failed", 1),
        ("", "pip", "Installing the lab export failed", 2),
        ("", "agentihooks", "The lab export exited 1: ci lab flakes: Could not connect to the endpoint URL", 3),
        ("", "", "", 3),
    ],
)
def test_a_lab_export_that_cannot_run_warns_once_and_leaves_the_step_green(
    tmp_path: Path, unset: str, fail: str, warning: str, made: int
) -> None:
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())
    [step] = [
        step for step in workflow["jobs"]["flake-budget"]["steps"] if step.get("name") == "Export the lab flake records"
    ]
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    for name, body in (("python", FAKE_PYTHON), ("agentihooks", FAKE_AGENTIHOOKS)):
        (fakes / name).write_text(body)
        (fakes / name).chmod(0o755)
    (tmp_path / "temp").mkdir()
    calls = tmp_path / "calls.log"
    env = {
        "PATH": f"{fakes}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "RUNNER_TEMP": str(tmp_path / "temp"),
        "CALLS": str(calls),
        "FAIL": fail,
        **dict.fromkeys(SETTINGS, "set"),
        **({unset: ""} if unset else {}),
    }

    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    printed = result.stdout.splitlines()
    warnings = [line for line in printed if line.startswith("::warning")]
    expected = [f"::warning title=Lab flakes::{warning}; the flake budget counts the Actions reports only."]
    assert warnings == (expected if warning else [])
    assert ("Lab flake rows: 2" in printed) is not bool(warning)
    called = calls.read_text().splitlines() if calls.exists() else []
    assert (
        called
        == [
            f"python -m venv {tmp_path}/temp/lab-flakes",
            "python -m pip install --quiet --constraint .github/test-requirements.lock -e .",
            f"agentihooks ci lab flakes --out {tmp_path}/home/flake-reports/lab",
        ][:made]
    )
