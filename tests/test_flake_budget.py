import json
from pathlib import Path

import yaml

from scripts import ci_budget
from tests import flake_budget

DAY = 24 * 3600
NOW = 1_791_700_000.0
TEST = "tests/test_page.py::test_the_fold_survives_a_reload"


def _report(test=TEST, run="7", outcome="flaky"):
    return {"test": test, "run": run, "run_attempt": "1", "attempts": 2, "outcome": outcome, "errors": ["E boom"]}


def test_a_flaky_report_joins_the_history_once_per_run():
    history = flake_budget.gather([], [_report(), _report(f"{TEST}[250]"), _report(run=8)], NOW)
    assert history == [{"test": TEST, "run": "7", "at": NOW}, {"test": TEST, "run": "8", "at": NOW}]
    assert flake_budget.gather(history, [_report()], NOW + 60) == history


def test_a_failed_report_is_not_a_flake():
    assert flake_budget.gather([], [_report(outcome="failed")], NOW) == []


def test_lines_older_than_seven_days_drop_out():
    edge = {"test": TEST, "run": "1", "at": NOW - 7 * DAY}
    inside = {"test": TEST, "run": "2", "at": NOW - 7 * DAY + 1}
    assert flake_budget.gather([edge, inside], [], NOW) == [inside]
    assert flake_budget.gather([edge], [_report(run="1")], NOW) == [{"test": TEST, "run": "1", "at": NOW}]


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
    assert capsys.readouterr().out.splitlines()[-1] == f"{TEST} passed on a retry in 1 dev runs: 20"
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
        f"{TEST} passed on a retry in 2 dev runs: 20, 21",
        f"::warning title=Flake budget::{TEST} was flaky in 2 dev runs: 20, 21",
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


def test_no_flake_reports_is_red(tmp_path, capsys):
    out = tmp_path / "out.json"
    argv = ["--history", str(tmp_path / "h.jsonl"), "--reports", str(tmp_path), "--out", str(out)]
    assert flake_budget.main(argv) == 1
    assert capsys.readouterr().out == f"::error::No flake reports under {tmp_path}\n"
    assert not out.exists()


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
    assert steps["Restore the flake history"]["uses"] == "actions/cache/restore@v4"
    assert steps["Restore the flake history"]["with"] == {
        "path": "~/flake-history",
        "key": key,
        "restore-keys": "flake-history-",
    }
    assert steps["Download this run's flake reports"]["with"] == {"pattern": "flakes-3.*", "path": "~/flake-reports"}
    assert steps["Count flaky tests over seven days"]["run"].startswith(
        "python -m tests.flake_budget --history ~/flake-history/history.jsonl --reports ~/flake-reports"
        " --out flake-budget.json"
    )
    assert steps["Count flaky tests over seven days"]["shell"] == "bash"
    assert steps["Publish the flake budget"]["with"]["name"] == "flake-budget"
    assert steps["Publish the flake budget"]["with"]["path"] == "flake-budget.json"
    assert steps["Publish the flake budget"]["with"]["if-no-files-found"] == "error"
    assert steps["Save the flake history"]["uses"] == "actions/cache/save@v4"
    assert steps["Save the flake history"]["with"] == {"path": "~/flake-history", "key": key}
    order = [
        "Restore the flake history",
        "Download this run's flake reports",
        "Count flaky tests over seven days",
        "Publish the flake budget",
        "Save the flake history",
    ]
    names = list(steps)
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
