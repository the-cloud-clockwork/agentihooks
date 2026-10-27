import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


def _reports(root):
    for version in ("3.11", "3.12"):
        durations = root / f"durations-{version}-1"
        durations.mkdir(parents=True)
        (durations / "durations.json").write_text(json.dumps({"test_ok": 1, "test_retry[a]": 3}))
        flakes = root / f"flakes-{version}-1"
        flakes.mkdir()
        records = [{"test": "test_retry[a]", "outcome": "flaky", "attempts": 3, "run": "123"}]
        if version == "3.12":
            records += [
                {"test": "test_retry[b]", "outcome": "flaky", "attempts": 2, "run": "123"},
                {"test": "test_bad", "outcome": "failed", "attempts": 3, "run": "123"},
            ]
        (flakes / "flakes.jsonl").write_text("\n".join(json.dumps(record) for record in records * 2) + "\n")


def test_summary_counts_test_executions_once_per_python_version_and_not_retry_attempts(tmp_path):
    from scripts.ci_gate_summary import counts

    _reports(tmp_path)
    (tmp_path / "flake-budget.json").write_text('{"flaky": 900}')
    assert counts(tmp_path) == {"ran": 6, "flaky": 3, "failed": 1}


def test_summary_without_test_reports_is_zero(tmp_path):
    from scripts.ci_gate_summary import counts

    assert counts(tmp_path) == {"ran": 0, "flaky": 0, "failed": 0}


def test_summary_counts_passing_tests_separately_in_each_python_version(tmp_path):
    from scripts.ci_gate_summary import counts

    for version in ("3.11", "3.12"):
        report_dir = tmp_path / f"durations-{version}-1"
        report_dir.mkdir()
        (report_dir / "durations.json").write_text('{"test_passes": 1, "test_also_passes": 2}')
    assert counts(tmp_path) == {"ran": 4, "flaky": 0, "failed": 0}


def test_summary_command_prints_the_artifact_counts(tmp_path, monkeypatch, capsys):
    from scripts.ci_gate_summary import main

    _reports(tmp_path)
    monkeypatch.setattr("sys.argv", ["ci_gate_summary", str(tmp_path)])
    main()
    assert capsys.readouterr().out == "Tests: ran 6, flaky 3, failed 1\n"


def test_required_summary_writes_current_artifact_counts_even_after_gate_failure(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    gate = workflow["jobs"].get("test-summary", workflow["jobs"]["gate-required"])
    step = next(step for step in gate["steps"] if step.get("name") == "Summarize this run's tests")
    assert step["if"] == "${{ always() }}"
    assert "flake-budget" not in gate["needs"]
    downloads = [step for step in gate["steps"] if step.get("uses", "").startswith("actions/download-artifact@")]
    assert {step["with"]["pattern"] for step in downloads} == {"durations-3.*", "flakes-3.*"}
    assert all(step["if"] == "${{ always() }}" for step in downloads if not step["name"].endswith(", attempt 2"))
    assert all("run-id" not in step["with"] for step in downloads)
    _reports(tmp_path / "test-reports")
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=dict(os.environ, GITHUB_WORKSPACE=str(ROOT), GITHUB_STEP_SUMMARY=str(summary)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert summary.read_text() == "Tests: ran 6, flaky 3, failed 1\n"


def test_test_summary_never_delays_required_grading():
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    gate = jobs["gate-required"]
    assert len(gate["steps"]) == 1, "Required grading must not check out or download summary artifacts"
    assert gate["steps"][0]["name"] == "Require every gate to succeed"
    summary = jobs["test-summary"]
    assert summary["if"] == "${{ always() }}"
    assert set(summary["needs"]) == {"unit", "unit-selected"}
    assert summary.get("name", "test-summary") != "Gate — Required"
    assert "test-summary" not in gate["needs"]
    assert all("test-summary" not in job.get("needs", []) for job in jobs.values())


def test_selected_tests_publish_the_existing_duration_and_flake_formats():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    steps = workflow["jobs"]["unit-selected"]["steps"]
    run = next(step for step in steps if step.get("name") == "Run the selected tests")
    assert "--store-durations --durations-path durations.json" in run["run"]
    uploads = [step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")]
    for prefix, filename in (("durations", "durations.json"), ("flakes", "flakes.jsonl")):
        upload = next(step for step in uploads if step["with"]["name"].startswith(prefix + "-"))
        assert filename in upload["with"]["path"]
        assert "!cancelled()" in upload["if"]
        assert "matrix.python-version" in upload["with"]["name"]
        assert "matrix.leg" in upload["with"]["name"]


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_summary_includes_final_failures_when_retries_are_disabled(tmp_path, phase):
    from _pytest.reports import TestReport

    from scripts.ci_gate_summary import counts
    from tests.flake_ledger import FlakeLedger

    report_dir = tmp_path / "flakes-3.12-1"
    report_dir.mkdir()
    ledger = FlakeLedger(report_dir / "flakes.jsonl")
    report = TestReport("test_once", ("test_probe.py", 1, "test_once"), {}, "failed", "failure", phase)
    ledger.pytest_runtest_logreport(report)
    ledger.pytest_runtest_logfinish(report.nodeid)
    assert counts(tmp_path) == {"ran": 1, "flaky": 0, "failed": 1}
