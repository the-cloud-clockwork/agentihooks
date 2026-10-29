import json

import pytest

from tests import shard_summary

pytestmark = pytest.mark.unit

COLLECTED = ["t.py::a", "t.py::b", "t.py::c", "t.py::d", "t.py::e", "t.py::f"]
PLAN = ["t.py::c", "t.py::d", "t.py::e", "t.py::f", "gone.py::x"]


def _summary(tmp_path, *shards, plan=PLAN, flakes=(), branch=None, runs=(5, 6)):
    durations = []
    for index, nodeids in enumerate(shards, 1):
        path = tmp_path / f"durations-3.12-{index}" / "durations.json"
        path.parent.mkdir()
        path.write_text(json.dumps(dict.fromkeys(nodeids, 0.1)))
        durations.append(path)
    collected = tmp_path / "collected.json"
    collected.write_text(json.dumps(COLLECTED))
    planned = tmp_path / "reused-tests.json"
    if plan is not None:
        planned.write_text(json.dumps({"run": 77, "tests": plan}))
    flake = tmp_path / "flakes.jsonl"
    flake.write_text("".join(json.dumps(record) + "\n" for record in flakes))
    branched = tmp_path / "reused-branch.json"
    if branch is not None:
        entries = [{"run": run, "attempt": 1, "commit": "c" * 40, "head": "d" * 40, "tests": branch} for run in runs]
        branched.write_text(json.dumps({"version": 1, "runs": entries, "tests": branch}))
    return shard_summary.summary("3.12", durations, collected, planned, [flake, tmp_path / "missing.jsonl"], branched)


def test_shards_that_reused_count_every_planned_collected_test(tmp_path):
    flakes = [{"outcome": "flaky"}, {"outcome": "failed"}]
    assert _summary(tmp_path, ["t.py::a@w1"], ["t.py::b"], flakes=flakes) == [
        "Python 3.12: ran 2, reused 4 from dev run 77, flaky 1"
    ]


def test_a_shard_that_fell_back_reports_zero_reused_and_its_full_run(tmp_path):
    assert _summary(tmp_path, ["t.py::a"], ["t.py::b", "t.py::c", "t.py::d@w2"]) == [
        "Python 3.12: ran 4, reused 2 from dev run 77, flaky 0",
        "durations-3.12-2 fell back: ran 3, reused 0",
    ]


def test_every_shard_falling_back_reuses_nothing(tmp_path):
    assert _summary(tmp_path, ["t.py::a", "t.py::c", "t.py::e"], ["t.py::b", "t.py::d", "t.py::f"]) == [
        "Python 3.12: ran 6, reused 0 from dev run 77, flaky 0",
        "durations-3.12-1 fell back: ran 3, reused 0",
        "durations-3.12-2 fell back: ran 3, reused 0",
    ]


def test_without_a_plan_nothing_is_reused(tmp_path):
    assert _summary(tmp_path, ["t.py::a"], ["t.py::b"], plan=None) == [
        "Python 3.12: ran 2, reused 0 from no plan, flaky 0"
    ]


def test_main_prints_the_summary(tmp_path, capsys):
    path = tmp_path / "durations-3.12-1" / "durations.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"t.py::a": 0.1}))
    collected = tmp_path / "collected.json"
    collected.write_text(json.dumps(COLLECTED))
    argv = [
        "--version",
        "3.12",
        "--durations",
        str(path),
        "--collected",
        str(collected),
        "--plan",
        str(tmp_path / "none"),
    ]
    assert shard_summary.main(argv) == 0
    assert capsys.readouterr().out == "Python 3.12: ran 1, reused 0 from no plan, flaky 0\n"


def test_without_a_pull_request_plan_the_line_is_the_one_printed_before(tmp_path):
    flakes = [{"outcome": "flaky"}]
    assert _summary(tmp_path, ["t.py::a@w1"], ["t.py::b"], flakes=flakes) == [
        "Python 3.12: ran 2, reused 4 from dev run 77, flaky 1"
    ]


def test_a_pull_request_plan_reports_its_reused_tests_and_runs(tmp_path):
    lines = _summary(tmp_path, ["t.py::a"], branch=["t.py::b", "gone.py::y"])
    assert lines == ["Python 3.12: ran 1, reused 4 from dev run 77, reused 1 from pull request runs 5 6, flaky 0"]


def test_a_test_both_plans_reuse_counts_once_from_dev(tmp_path):
    lines = _summary(tmp_path, ["t.py::a"], branch=["t.py::b", "t.py::c"], runs=(5,))
    assert lines == ["Python 3.12: ran 1, reused 4 from dev run 77, reused 1 from pull request runs 5, flaky 0"]


def test_a_shard_that_ran_a_test_reused_from_a_pull_request_run_fell_back(tmp_path):
    lines = _summary(tmp_path, ["t.py::a"], ["t.py::b"], plan=None, branch=["t.py::b"])
    assert lines == [
        "Python 3.12: ran 2, reused 0 from no plan, reused 0 from pull request runs 5 6, flaky 0",
        "durations-3.12-2 fell back: ran 1, reused 0",
    ]


def test_a_pull_request_plan_with_no_run_reuses_nothing(tmp_path):
    lines = _summary(tmp_path, ["t.py::a", "t.py::b"], branch=[], runs=())
    assert lines == ["Python 3.12: ran 2, reused 4 from dev run 77, reused 0 from no pull request run, flaky 0"]


def test_main_prints_the_pull_request_reuse(tmp_path, capsys):
    path = tmp_path / "durations-3.12-1" / "durations.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"t.py::a": 0.1}))
    collected = tmp_path / "collected.json"
    collected.write_text(json.dumps(COLLECTED))
    branch = tmp_path / "reused-branch.json"
    branch.write_text(json.dumps({"version": 1, "runs": [{"run": 9, "tests": ["t.py::b"]}], "tests": ["t.py::b"]}))
    argv = ["--version", "3.12", "--durations", str(path), "--collected", str(collected), "--plan", str(tmp_path / "x")]
    assert shard_summary.main([*argv, "--branch-plan", str(branch)]) == 0
    assert capsys.readouterr().out == (
        "Python 3.12: ran 1, reused 0 from no plan, reused 1 from pull request runs 9, flaky 0\n"
    )
