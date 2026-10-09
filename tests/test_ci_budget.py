import json
from pathlib import Path

import pytest
import yaml

from scripts import ci_budget
from scripts.ci_budget.__main__ import main

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
START = "2026-10-09T07:00:00Z"


def _job(name, created, completed, conclusion="success"):
    return {
        "name": name,
        "created_at": f"2026-10-09T07:{created}Z",
        "started_at": f"2026-10-09T07:{created}Z",
        "completed_at": completed and f"2026-10-09T07:{completed}Z",
        "conclusion": conclusion,
    }


JOBS = [
    _job("durations", "00:05", "00:40"),
    _job("unit (3.12, 1)", "02:00", "05:00"),
    _job("unit (3.11, 4)", "02:10", "06:30"),
    _job("swarm-image / smoke", "00:05", "01:00"),
    _job("swarm-image / hive-join", "00:05", "01:40"),
    _job("mutation", "00:05", "10:05"),
    _job("helm-kind / kind", "00:30", "00:30", "skipped"),
    _job("Gate — Required", "11:00", None, None),
    _job("stage-budget", "10:30", None, None),
    _job("brand-new", "00:05", "00:15"),
]


def test_a_stage_spans_first_job_creation_to_last_job_end():
    spent = ci_budget.stages(JOBS)
    assert spent == {
        "durations": 35,
        "unit": 270,
        "swarm-image": 95,
        "mutation": 600,
        "brand-new": 10,
    }


def test_report_orders_stages_slowest_first_and_flags_over_budget_and_unbudgeted():
    result = ci_budget.report({"run_started_at": START}, JOBS, ci_budget.seconds("2026-10-09T07:16:00Z"))
    assert result["total"] == 960
    assert [row["stage"] for row in result["stages"]] == ["mutation", "unit", "swarm-image", "durations", "brand-new"]
    assert [ci_budget.verdict(row) for row in result["stages"]] == ["over", "ok", "ok", "ok", "no budget"]
    lines = ci_budget.render(result)
    assert lines[1].split() == ["mutation", "10m00s", "8m00s", "over"]
    assert lines[-1].split()[-3:] == ["16m00s", "15m00s", "over"]


def test_every_job_of_the_tests_workflow_has_a_budget():
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    assert set(jobs) == set(ci_budget.BUDGETS)


def test_the_longest_chain_of_budgets_to_gate_required_fits_fifteen_minutes():
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]

    def chain(job):
        needs = jobs[job].get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        return ci_budget.BUDGETS[job] + max((chain(need) for need in needs), default=0)

    assert chain("gate-required") <= ci_budget.RUN_BUDGET_S


def _files(tmp_path, jobs):
    run, listed = tmp_path / "run.json", tmp_path / "jobs.jsonl"
    run.write_text(json.dumps({"run_started_at": START}))
    listed.write_text("".join(json.dumps(job) + "\n" for job in jobs))
    return ["--run", str(run), "--jobs", str(listed)]


def test_cli_prints_the_table_summary_and_annotations(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = main(_files(tmp_path, JOBS), now=lambda: ci_budget.seconds("2026-10-09T07:16:00Z"))
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[1].split() == ["mutation", "10m00s", "8m00s", "over"]
    assert "::warning title=Stage over budget::mutation took 10m00s against its budget of 8m00s" in out
    assert "::warning title=Stage without a budget::brand-new took 0m10s and has no budget" in out
    assert "::error title=Run over fifteen minutes::push to this report took 16m00s against 15m00s" in out
    assert "| mutation | 10m00s | 8m00s | over |" in summary.read_text()


def test_cli_inside_every_budget_prints_no_annotation(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    main(_files(tmp_path, JOBS[:2]), now=lambda: ci_budget.seconds("2026-10-09T07:06:00Z"))
    out = capsys.readouterr().out
    assert "::" not in out
    assert out.splitlines()[-2].split()[-3:] == ["6m00s", "15m00s", "ok"]


def test_cli_fails_when_the_jobs_list_is_empty(tmp_path, capsys):
    assert main(_files(tmp_path, [])) == 1
    assert "lists no jobs" in capsys.readouterr().out


def test_cli_fails_when_the_jobs_were_not_read(tmp_path):
    with pytest.raises(FileNotFoundError):
        main(["--run", str(tmp_path / "run.json"), "--jobs", str(tmp_path / "jobs.jsonl")])


def test_stage_budget_runs_after_every_gate_need_and_gate_required_needs_it():
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    job, gate = jobs[ci_budget.SELF], jobs["gate-required"]
    assert set(job["needs"]) == set(gate["needs"]) - {ci_budget.SELF}
    assert ci_budget.SELF in gate["needs"]
    assert job["if"] == "${{ always() }}"
    assert "permissions" not in job and "continue-on-error" not in job
    mint, read, report = job["steps"][1:]
    assert mint["uses"] == "actions/create-github-app-token@v3.2.0"
    assert mint["with"]["permission-actions"] == "read"
    assert read["env"] == {"GH_TOKEN": "${{ steps.app-token.outputs.token }}"}
    assert "/attempts/$GITHUB_RUN_ATTEMPT/jobs" in read["run"]
    assert "env" not in report and "if" not in report
    assert report["run"].startswith("python3 -m scripts.ci_budget --run")
