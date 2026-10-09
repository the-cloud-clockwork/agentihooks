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


def _workflow_jobs():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def test_seconds_reads_github_timestamps_as_utc():
    assert ci_budget.seconds("1970-01-01T00:01:00Z") == 60


@pytest.mark.parametrize(
    ("name", "stage"),
    [
        ("Gate — Required", "gate-required"),
        ("unit (3.12, 1)", "unit"),
        ("swarm-image / hive-join", "swarm-image"),
        ("a b / c", "a b"),
        ("a b (1)", "a b"),
    ],
)
def test_stage_of_names_the_job_or_reusable_workflow_id(name, stage):
    assert ci_budget.stage_of(name) == stage


def test_a_stage_spans_first_job_creation_to_last_job_end():
    assert ci_budget.stages(JOBS) == {
        "durations": 35,
        "unit": 270,
        "swarm-image": 95,
        "mutation": 600,
        "brand-new": 10,
    }


def test_report_orders_stages_slowest_first_then_by_name():
    jobs = [_job("wiring", "00:00", "00:10"), _job("size", "00:00", "00:10"), _job("lint", "00:00", "00:20")]
    result = ci_budget.report({"run_started_at": START}, jobs, ci_budget.seconds("2026-10-09T07:01:00Z"))
    assert result == {
        "total": 60,
        "stages": [
            {"stage": "lint", "seconds": 20, "budget": 150},
            {"stage": "size", "seconds": 10, "budget": 90},
            {"stage": "wiring", "seconds": 10, "budget": 60},
        ],
    }


def test_a_stage_at_its_budget_and_a_run_at_fifteen_minutes_are_inside_budget():
    assert ci_budget.verdict({"seconds": 60, "budget": 60}) == "ok"
    assert ci_budget.verdict({"seconds": 61, "budget": 60}) == "over"
    assert ci_budget.verdict({"seconds": 1, "budget": None}) == "no budget"
    assert ci_budget.run_state(900) == "ok"
    assert ci_budget.run_state(901) == "over"


def test_render_prints_every_stage_and_the_run_against_the_budget():
    result = ci_budget.report({"run_started_at": START}, JOBS, ci_budget.seconds("2026-10-09T07:16:00Z"))
    assert [line.split() for line in ci_budget.render(result)] == [
        ["stage", "wall", "budget", "verdict"],
        ["mutation", "10m00s", "8m00s", "over"],
        ["unit", "4m30s", "5m00s", "ok"],
        ["swarm-image", "1m35s", "3m00s", "ok"],
        ["durations", "0m35s", "1m00s", "ok"],
        ["brand-new", "0m10s", "none", "no", "budget"],
        ["push", "to", "this", "report", "16m00s", "15m00s", "over"],
    ]


def test_every_job_of_the_tests_workflow_has_a_budget():
    assert set(_workflow_jobs()) == set(ci_budget.BUDGETS)


def test_the_longest_chain_of_budgets_to_gate_required_fits_fifteen_minutes():
    jobs = _workflow_jobs()

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
    jobs = [JOBS[0], JOBS[5], JOBS[-1]]
    end = ci_budget.seconds("2026-10-09T07:16:00Z")
    assert main(_files(tmp_path, jobs), now=lambda: end) == 0
    assert capsys.readouterr().out == "\n".join(
        [
            *ci_budget.render(ci_budget.report({"run_started_at": START}, jobs, end)),
            "::warning title=Stage over budget::mutation took 10m00s against its budget of 8m00s",
            "::warning title=Stage without a budget::brand-new took 0m10s and has no budget",
            "::error title=Run over fifteen minutes::push to this report took 16m00s against 15m00s",
            "",
        ]
    )
    assert summary.read_text() == "\n".join(
        [
            "## Stage budget",
            "",
            "| Stage | Wall | Budget | Verdict |",
            "| --- | ---: | ---: | --- |",
            "| mutation | 10m00s | 8m00s | over |",
            "| durations | 0m35s | 1m00s | ok |",
            "| brand-new | 0m10s | none | no budget |",
            "| push to this report | 16m00s | 15m00s | over |",
            "",
        ]
    )


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
    args = _files(tmp_path, JOBS)
    (tmp_path / "jobs.jsonl").unlink()
    with pytest.raises(FileNotFoundError):
        main(args)


@pytest.mark.parametrize("dropped", ["--run", "--jobs"])
def test_cli_requires_both_files(tmp_path, dropped):
    args = _files(tmp_path, JOBS)
    index = args.index(dropped)
    with pytest.raises(SystemExit):
        main(args[:index] + args[index + 2 :])


def test_stage_budget_runs_after_every_gate_need_and_gate_required_needs_it():
    jobs = _workflow_jobs()
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
