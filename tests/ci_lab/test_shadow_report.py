from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import scripts.ci_lab.__main__ as cli
from scripts import ci_lab_verdict
from scripts.ci_lab import checks, shadow, shadow_report

HEAD = "a" * 40
OTHER = "b" * 40
THIRD = "c" * 40
REPOSITORY = "owner/repo"
RUNS = f"repos/{REPOSITORY}/actions/workflows/test.yml/runs?event=pull_request&status=completed"
NONE: frozenset[str] = frozenset()
APP = "agentihooks-lab"
SOURCES = shadow.Sources(NONE, APP)
LINT = shadow.Sources(frozenset({"lint"}), APP)


def stamp(minute: int) -> str:
    return f"2026-11-18T00:{minute:02d}:00Z"


def at(minute: int) -> datetime:
    return datetime.fromisoformat(stamp(minute))


def job(name: str, conclusion: str = "success", minute: int = 10, *, status: str = "completed") -> dict[str, Any]:
    done = status == "completed"
    return {
        "id": 1,
        "name": name,
        "status": status,
        "conclusion": conclusion if done else None,
        "completed_at": stamp(minute) if done else None,
        "app": {"slug": "github-actions"},
    }


def lab(
    gate: str = "tests",
    conclusion: str = "success",
    minute: int = 5,
    *,
    status: str = "completed",
    run_id: int = 2,
    slug: str = "agentihooks-lab",
) -> dict[str, Any]:
    run = job(f"lab-shadow/{gate}", conclusion, minute, status=status)
    return {**run, "id": run_id, "app": {"slug": slug}}


def graded(
    sha: str, actions: str, lab_outcome: str | None, actions_minute: int = 10, lab_minute: int = 5
) -> shadow_report.Head:
    verdict = None if lab_outcome is None else shadow_report.Verdict(lab_outcome, at(lab_minute))
    return shadow_report.Head(sha, shadow_report.Verdict(actions, at(actions_minute)), verdict)


def waiting(minute: int | None, *, run_id: int = 3, slug: str = "github-actions") -> dict[str, Any]:
    started = None if minute is None else stamp(minute)
    return {**job("lab-verdict"), "id": run_id, "started_at": started, "app": {"slug": slug}}


def waited(sha: str, lab_at: datetime | None, started: datetime | None, actions: str = "pass") -> shadow_report.Head:
    verdict = None if lab_at is None else shadow_report.Verdict("pass", lab_at)
    return shadow_report.Head(sha, shadow_report.Verdict(actions, at(40)), verdict, started)


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_a_lab_verdict_that_disagrees_with_actions_is_a_mismatch_that_fails_the_rule() -> None:
    runs = [job("unit (3.12, 1)", "failure", 10), job("unit (3.12, 2)", "timed_out", 12), lab("tests", "success", 5)]
    found = shadow_report.head(HEAD, runs, "tests", SOURCES)
    assert found == graded(HEAD, "fail", "pass", 12, 5)
    assert found is not None
    assert shadow_report.mismatch(found) is True
    assert shadow_report.tally([found]) == shadow_report.Tally(1, 1, 1, 1)
    assert shadow_report.holds(shadow_report.tally([found]), shadow_report.rule(1, "merge_group")) is False
    agreed = graded(HEAD, "pass", "pass")
    assert shadow_report.mismatch(agreed) is False
    assert shadow_report.holds(shadow_report.tally([agreed]), shadow_report.rule(1, "merge_group")) is True
    lab_failed = shadow_report.head(HEAD, [job("lint"), lab("lint", "failure", 4)], "lint", LINT)
    assert lab_failed == graded(HEAD, "pass", "fail", 10, 4)
    assert lab_failed is not None
    assert shadow_report.mismatch(lab_failed) is True


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_a_head_without_a_lab_verdict_counts_against_presence() -> None:
    actions = [job("unit (3.12, 1)")]
    assert shadow_report.lab(actions, "tests", APP) is None
    assert shadow_report.lab([*actions, lab(status="in_progress")], "tests", APP) is None
    assert shadow_report.lab([*actions, lab(conclusion="cancelled")], "tests", APP) is None
    assert shadow_report.lab([*actions, lab(slug="github-actions")], "tests", APP) is None
    assert shadow_report.lab([*actions, lab(slug="another-app")], "tests", APP) is None
    assert shadow_report.lab([*actions, lab(slug="another-app")], "tests", "another-app") == (
        shadow_report.Verdict("pass", at(5))
    )
    assert shadow_report.lab([*actions, lab("lint")], "tests", APP) is None
    newest = [lab(conclusion="failure", minute=3, run_id=7), lab(conclusion="success", minute=4, run_id=9)]
    assert shadow_report.lab(newest, "tests", APP) == shadow_report.Verdict("pass", at(4))
    assert shadow_report.lab(list(reversed(newest)), "tests", APP) == shadow_report.Verdict("pass", at(4))
    missing = graded(HEAD, "fail", None)
    assert shadow_report.mismatch(missing) is False
    assert shadow_report.first(missing) is False
    assert shadow_report.rule(50, "merge_group") == shadow_report.Rule(50, 48, 45)
    assert shadow_report.rule(10, "merge_group") == shadow_report.Rule(10, 10, 9)
    assert shadow_report.rule(1, "merge_group") == shadow_report.Rule(1, 1, 1)
    assert shadow_report.rule(5, "merge_group") == shadow_report.Rule(5, 5, 5)
    two_missing = [graded(HEAD, "pass", None)] * 2 + [graded(HEAD, "pass", "pass")] * 48
    assert shadow_report.tally(two_missing) == shadow_report.Tally(50, 48, 0, 48)
    assert shadow_report.holds(shadow_report.tally(two_missing), shadow_report.rule(50, "merge_group")) is True
    three_missing = [graded(HEAD, "pass", None)] * 3 + [graded(HEAD, "pass", "pass")] * 47
    assert shadow_report.holds(shadow_report.tally(three_missing), shadow_report.rule(50, "merge_group")) is False
    assert shadow_report.holds(shadow_report.tally(two_missing[1:]), shadow_report.rule(50, "merge_group")) is False


def listed(gate: str, conclusion: str, minute: int, *, run_id: int = 2, slug: str = APP) -> dict[str, Any]:
    return {**lab(gate, conclusion, minute, run_id=run_id, slug=slug), "name": checks.name(gate, shadow=False)}


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_the_acceptance_report_reads_the_lab_verdict_only_under_the_shadow_name() -> None:
    runs = [job("lint"), listed("lint", "failure", 5)]
    assert shadow_report.lab(runs, "lint", APP) is None
    assert shadow_report.head(HEAD, runs, "lint", LINT) == graded(HEAD, "pass", None)
    answers = {
        f"{RUNS}&per_page=100&page=1": {"workflow_runs": [{"head_sha": HEAD}]},
        f"repos/{REPOSITORY}/commits/{HEAD}/check-runs?filter=latest&per_page=100&page=1": {"check_runs": runs},
    }
    collected = shadow_report.collect(answers.__getitem__, REPOSITORY, shadow_report.PULL_REQUEST, "lint", LINT, 1)
    assert collected == [graded(HEAD, "pass", None)]
    assert shadow_report.tally(collected) == shadow_report.Tally(1, 0, 0, 0)


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_a_listed_gate_reads_the_newest_lab_verdict_under_either_name() -> None:
    assert f"{shadow_report.LISTED_NAME}lint" == checks.name("lint", shadow=False) == "lab/lint"
    assert f"{shadow.LAB_NAME}lint" == checks.name("lint", shadow=True)
    posted = [listed("lint", "failure", 6, run_id=9), lab("lint", "success", 4, run_id=7)]
    assert shadow_report.lab(posted, "lint", APP, listed=True) == shadow_report.Verdict("fail", at(6))
    assert shadow_report.lab(posted, "lint", APP) == shadow_report.Verdict("pass", at(4))
    older = [listed("lint", "failure", 6, run_id=7), lab("lint", "success", 4, run_id=9)]
    assert shadow_report.lab(older, "lint", APP, listed=True) == shadow_report.Verdict("pass", at(4))
    assert shadow_report.lab([listed("type-gate", "failure", 6)], "lint", APP, listed=True) is None
    assert shadow_report.lab([listed("lint", "failure", 6, slug="another-app")], "lint", APP, listed=True) is None
    runs = [job("lint"), *posted]
    assert shadow_report.head(HEAD, runs, "lint", LINT, listed=True) == graded(HEAD, "pass", "fail", 10, 6)
    assert shadow_report.head(HEAD, runs, "lint", LINT) == graded(HEAD, "pass", "pass", 10, 4)
    assert shadow_report.head(HEAD, [job("lint", "skipped"), *posted], "lint", LINT, listed=True) is None


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_a_cancelled_actions_job_takes_its_head_out_of_the_window() -> None:
    assert shadow_report.actions([job("unit (3.12, 1)"), job("unit (3.12, 2)", "cancelled")], "tests", NONE) is None
    assert (
        shadow_report.actions([job("unit (3.12, 1)"), job("unit (3.12, 2)", status="in_progress")], "tests", NONE)
        is None
    )
    assert shadow_report.actions([job("unit (3.12, 1)", "skipped")], "tests", NONE) is None
    assert shadow_report.actions([job("lint")], "tests", NONE) is None
    assert shadow_report.actions([lab()], "tests", NONE) is None
    mixed = [job("unit (3.12, 1)", "skipped", 20), job("unit (3.12, 2)", "success", 11), job("lint", "failure", 30)]
    assert shadow_report.actions(mixed, "tests", frozenset({"lint"})) == shadow_report.Verdict("pass", at(11))
    answers = {
        f"{RUNS}&per_page=100&page=1": {
            "workflow_runs": [{"head_sha": HEAD}, {"head_sha": OTHER}, {"head_sha": HEAD}, {"head_sha": THIRD}]
        },
        f"repos/{REPOSITORY}/commits/{HEAD}/check-runs?filter=latest&per_page=100&page=1": {
            "check_runs": [job("unit (3.12, 1)", "cancelled"), lab()]
        },
        f"repos/{REPOSITORY}/commits/{OTHER}/check-runs?filter=latest&per_page=100&page=1": {
            "check_runs": [job("unit (3.12, 1)", "failure"), lab()]
        },
        f"repos/{REPOSITORY}/commits/{THIRD}/check-runs?filter=latest&per_page=100&page=1": {
            "check_runs": [job("unit (3.12, 1)")]
        },
    }
    asked: list[str] = []

    def fetch(path: str) -> dict[str, Any]:
        asked.append(path)
        return answers[path]

    found = shadow_report.collect(fetch, REPOSITORY, "pull_request", "tests", SOURCES, 2)
    assert found == [graded(OTHER, "fail", "pass"), graded(THIRD, "pass", None)]
    assert asked == list(answers)
    assert shadow_report.collect(fetch, REPOSITORY, "pull_request", "tests", SOURCES, 1) == [
        graded(OTHER, "fail", "pass")
    ]


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_a_lab_verdict_completing_after_the_actions_jobs_is_late() -> None:
    runs = [job("unit (3.12, 1)", minute=9), job("unit (3.12, 2)", minute=12)]
    late = shadow_report.head(HEAD, [*runs, lab(minute=15)], "tests", SOURCES)
    assert late == graded(HEAD, "pass", "pass", 12, 15)
    assert late is not None
    assert shadow_report.first(late) is False
    assert shadow_report.first(graded(HEAD, "pass", "pass", 12, 12)) is False
    assert shadow_report.first(graded(HEAD, "pass", "pass", 12, 11)) is True
    window = shadow_report.rule(50, "merge_group")
    on_time = [graded(HEAD, "pass", "pass", 12, 11)] * 45 + [graded(HEAD, "pass", "pass", 12, 13)] * 5
    assert shadow_report.tally(on_time) == shadow_report.Tally(50, 50, 0, 45)
    assert shadow_report.holds(shadow_report.tally(on_time), window) is True
    too_late = [graded(HEAD, "pass", "pass", 12, 11)] * 44 + [graded(HEAD, "pass", "pass", 12, 13)] * 6
    assert shadow_report.holds(shadow_report.tally(too_late), window) is False


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_a_head_keeps_when_its_newest_lab_verdict_job_started() -> None:
    runs = [job("lint"), lab("lint", minute=4)]
    found = shadow_report.head(HEAD, [*runs, waiting(2, run_id=7), waiting(3, run_id=9)], "lint", LINT)
    assert found == shadow_report.Head(
        HEAD, shadow_report.Verdict("pass", at(10)), shadow_report.Verdict("pass", at(4)), at(3)
    )
    assert shadow_report.head(HEAD, [waiting(3, run_id=9), *runs, waiting(2, run_id=7)], "lint", LINT) == found
    older = shadow_report.head(HEAD, [*runs, waiting(2, run_id=9), waiting(3, run_id=7)], "lint", LINT)
    assert older is not None
    assert older.started == at(2)
    unstarted = [*runs, waiting(2, run_id=7), waiting(None, run_id=9)]
    for without in (runs, [*runs, waiting(2, slug=APP)], unstarted):
        assert shadow_report.head(HEAD, without, "lint", LINT) == graded(HEAD, "pass", "pass", 10, 4)


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_a_lab_verdict_job_whose_check_run_names_no_start_leaves_the_head_readable() -> None:
    bare = {key: value for key, value in waiting(2).items() if key != "started_at"}
    found = shadow_report.head(HEAD, [job("lint"), listed("lint", "failure", 6), bare], "lint", LINT, listed=True)
    assert found == graded(HEAD, "pass", "fail", 10, 6)
    assert found is not None
    assert shadow_report.mismatch(found) is True


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_a_lab_verdict_is_inside_the_wait_up_to_its_last_second() -> None:
    last = at(0) + timedelta(seconds=ci_lab_verdict.WAIT_S)
    assert last == at(20)
    assert shadow_report.inside(waited(HEAD, last, at(0))) is True
    assert shadow_report.inside(waited(HEAD, last + timedelta(seconds=1), at(0))) is False
    assert shadow_report.inside(waited(HEAD, at(0) - timedelta(seconds=1), at(0))) is True
    assert shadow_report.inside(waited(HEAD, None, at(0))) is False
    assert shadow_report.inside(waited(HEAD, at(1), None)) is False
    assert shadow_report.inside(graded(HEAD, "pass", "pass")) is False


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_the_pull_request_rule_asks_for_lab_verdicts_inside_the_wait_on_48_of_every_50_heads() -> None:
    assert shadow_report.rule(50, "pull_request") == shadow_report.Rule(50, 48, 45, 48)
    assert shadow_report.rule(10, "pull_request") == shadow_report.Rule(10, 10, 9, 10)
    assert shadow_report.rule(25, "pull_request") == shadow_report.Rule(25, 24, 23, 24)
    assert shadow_report.rule(50, "merge_group") == shadow_report.Rule(50, 48, 45)
    window = shadow_report.rule(50, "pull_request")
    on_time, late = waited(HEAD, at(20), at(0)), waited(HEAD, at(21), at(0))
    enough = [on_time] * 48 + [late] * 2
    assert shadow_report.tally(enough) == shadow_report.Tally(50, 50, 0, 50, 48)
    assert shadow_report.holds(shadow_report.tally(enough), window) is True
    short = [on_time] * 47 + [late] * 3
    assert shadow_report.tally(short) == shadow_report.Tally(50, 50, 0, 50, 47)
    assert shadow_report.holds(shadow_report.tally(short), window) is False
    assert shadow_report.holds(shadow_report.tally(short), shadow_report.rule(50, "merge_group")) is True


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_a_lost_runners_unit_job_counts_by_its_rerun() -> None:
    assert shadow_report.rerun("unit (3.12, 1)") == "unit rerun (3.12, 1)"
    assert shadow_report.rerun("unit (3.12, 1) (retry)") == "unit rerun (3.12, 1) (retry)"
    assert shadow_report.rerun("unit") is None
    assert shadow_report.rerun("unit-lost") is None
    assert shadow_report.rerun("mutation (1)") is None
    retried = [
        job("unit (3.12, 1)", "failure", 8),
        job("unit rerun (3.12, 1)", "success", 14),
        job("unit-lost", "success", 9),
    ]
    assert shadow_report.actions(retried, "tests", NONE) == shadow_report.Verdict("pass", at(14))
    failed_again = [job("unit (3.12, 1)", "failure", 8), job("unit rerun (3.12, 1)", "failure", 14)]
    assert shadow_report.actions(failed_again, "tests", NONE) == shadow_report.Verdict("fail", at(14))
    unrelated = [job("unit (3.12, 1)", "failure", 8), job("unit rerun (3.12, 2)", "success", 14)]
    assert shadow_report.actions(unrelated, "tests", NONE) == shadow_report.Verdict("fail", at(14))


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_the_table_prints_a_row_per_head_and_the_rule_counts() -> None:
    heads = [
        graded(HEAD, "pass", "pass", 12, 11),
        graded(OTHER, "fail", "pass", 12, 13),
        graded(THIRD, "pass", None),
    ]
    assert shadow_report.render("tests", "merge_group", heads, shadow_report.rule(3, "merge_group")) == (
        "gate tests event merge_group\n"
        "head          actions  lab      order  agreement\n"
        "aaaaaaaaaaaa  pass     pass     first  match\n"
        "bbbbbbbbbbbb  fail     pass     late   mismatch\n"
        "cccccccccccc  pass     missing\n"
        "heads 3 of 3\n"
        "lab verdict present 2 of at least 3\n"
        "mismatches 1 of at most 0\n"
        "lab verdict first 1 of at least 3\n"
        "rule fails\n"
    )
    one = shadow_report.render("lint", "merge_group", heads[:1], shadow_report.rule(1, "merge_group"))
    assert one.splitlines()[0] == "gate lint event merge_group"
    assert one.endswith("lab verdict first 1 of at least 1\nrule holds\n")


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_the_pull_request_report_prints_the_count_inside_the_wait_and_the_seconds_each_head_took() -> None:
    heads = [
        waited(HEAD, at(11), at(0)),
        waited(OTHER, at(21), at(0), "fail"),
        waited(THIRD, at(11), at(30)),
        graded("d" * 40, "pass", "pass", 12, 13),
        waited("e" * 40, None, at(0)),
    ]
    assert shadow_report.render("lint", "pull_request", heads, shadow_report.rule(5, "pull_request")) == (
        "gate lint event pull_request\n"
        "head          actions  lab      order  agreement  seconds\n"
        "aaaaaaaaaaaa  pass     pass     first  match      660\n"
        "bbbbbbbbbbbb  fail     pass     first  mismatch   1260\n"
        "cccccccccccc  pass     pass     first  match      -1140\n"
        "dddddddddddd  pass     pass     late   match\n"
        "eeeeeeeeeeee  pass     missing\n"
        "heads 5 of 5\n"
        "lab verdict present 4 of at least 5\n"
        "mismatches 1 of at most 0\n"
        "lab verdict first 3 of at least 5\n"
        "lab verdict inside the wait 2 of at least 5\n"
        "rule fails\n"
    )
    unwaited = shadow_report.render("lint", "merge_group", heads, shadow_report.rule(5, "merge_group"))
    assert unwaited.splitlines()[1:3] == [
        "head          actions  lab      order  agreement",
        "aaaaaaaaaaaa  pass     pass     first  match",
    ]
    assert unwaited.endswith("lab verdict first 3 of at least 5\nrule fails\n")
    half_a_second_late = waited(HEAD, at(20) + timedelta(milliseconds=500), at(0))
    assert shadow_report.inside(half_a_second_late) is False
    assert shadow_report.timed(half_a_second_late) == "aaaaaaaaaaaa  pass     pass     first  match      1201"


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_the_merge_group_report_keeps_three_counts_and_five_columns() -> None:
    heads = [graded(HEAD, "pass", "pass", 12, 11), graded(OTHER, "pass", None)]
    window = shadow_report.rule(2, "merge_group")
    assert window == shadow_report.Rule(2, 2, 2)
    assert shadow_report.render("lint", "merge_group", heads, window) == (
        "gate lint event merge_group\n"
        "head          actions  lab      order  agreement\n"
        "aaaaaaaaaaaa  pass     pass     first  match\n"
        "bbbbbbbbbbbb  pass     missing\n"
        "heads 2 of 2\n"
        "lab verdict present 1 of at least 2\n"
        "mismatches 0 of at most 0\n"
        "lab verdict first 1 of at least 2\n"
        "rule fails\n"
    )
    assert shadow_report.holds(shadow_report.Tally(50, 48, 0, 45), shadow_report.Rule(50, 48, 45)) is True
    assert shadow_report.holds(shadow_report.Tally(50, 48, 0, 44), shadow_report.Rule(50, 48, 45)) is False


def answer(stdout: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(stdout))


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_the_report_command_reads_live_heads_through_the_github_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = f"repos/{REPOSITORY}/actions/workflows/test.yml/runs?event=merge_group&status=completed"
    checks = f"repos/{REPOSITORY}/commits/{HEAD}/check-runs?filter=latest"
    answers = {
        f"{runs}&per_page=100&page=1": {"workflow_runs": [{"head_sha": HEAD}] * 100},
        f"{runs}&per_page=100&page=2": {"workflow_runs": []},
        f"{checks}&per_page=100&page=1": {"check_runs": [job(f"other {index}") for index in range(100)]},
        f"{checks}&per_page=100&page=2": {"check_runs": [job("lint", minute=10), lab("lint", minute=6)]},
    }
    calls: list[tuple[list[str], dict[str, object]]] = []

    def gh_api(args: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, options))
        return answer(answers[args[0]])

    def static_jobs(root: Path) -> frozenset[str]:
        assert root == shadow_report.ROOT
        return frozenset({"lint"})

    monkeypatch.setattr(shadow_report, "gh_api", gh_api)
    monkeypatch.setattr(shadow, "static_jobs", static_jobs)
    environ = {"GITHUB_REPOSITORY": REPOSITORY, "AGENTIHOOKS_LAB_APP": APP}
    argv = ["shadow", "report", "--gate", "lint", "--heads", "1", "--event", "merge_group"]
    assert cli.main(argv, environ) == 0
    out = capsys.readouterr().out
    assert out.startswith(
        "gate lint event merge_group\nhead          actions  lab      order  agreement\naaaaaaaaaaaa  pass"
    )
    assert out.endswith("rule holds\n")
    first_page, second_page, first_checks, second_checks = ([path] for path in answers)
    assert [args for args, _ in calls] == [first_page, first_checks, second_checks]
    assert all(options == {"timeout": 30.0, "text": True} for _, options in calls)
    calls.clear()
    flagged = ["shadow", "report", "--gate=lint", "--heads", "2", "--event", "merge_group", "--app", APP]
    assert cli.main(flagged, {"GITHUB_REPOSITORY": REPOSITORY}) == 1
    assert capsys.readouterr().out.endswith(
        "heads 1 of 2\nlab verdict present 1 of at least 2\nmismatches 0 of at most 0\n"
        "lab verdict first 1 of at least 2\nrule fails\n"
    )
    assert [args for args, _ in calls] == [first_page, first_checks, second_checks, second_page]


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_the_report_command_names_its_repository_and_refuses_an_empty_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[str] = []

    def fetch(path: str) -> dict[str, Any]:
        asked.append(path)
        return {"workflow_runs": []}

    monkeypatch.setattr(shadow, "static_jobs", lambda root: NONE)
    default = (
        "repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs?event=pull_request&status=completed"
        "&per_page=100&page=1"
    )
    assert shadow_report.main(["--gate", "tests", "--app", APP], {"GITHUB_REPOSITORY": ""}, fetch) == 1
    assert asked == [default]
    capsys.readouterr()
    asked.clear()
    assert shadow_report.main(["--gate", "tests", "--app", APP], {}, fetch) == 1
    assert asked == [default]
    assert capsys.readouterr().out.endswith(
        "heads 0 of 50\nlab verdict present 0 of at least 48\nmismatches 0 of at most 0\n"
        "lab verdict first 0 of at least 45\nlab verdict inside the wait 0 of at least 48\nrule fails\n"
    )
    with pytest.raises(SystemExit) as refused:
        shadow_report.main(["--gate", "tests", "--heads", "0"], {}, fetch)
    assert refused.value.code == 2
    error = capsys.readouterr().err
    assert error.startswith("usage: python -m scripts.ci_lab shadow report")
    assert error.endswith("error: --heads must be at least 1\n")
    with pytest.raises(SystemExit) as pushed:
        shadow_report.main(["--gate", "tests", "--event", "push", "--app", APP], {}, fetch)
    assert pushed.value.code == 2
    assert "error: argument --event: invalid choice: 'push' (choose from " in capsys.readouterr().err
    with pytest.raises(SystemExit) as gateless:
        shadow_report.main(["--heads", "3", "--app", APP], {}, fetch)
    assert gateless.value.code == 2
    capsys.readouterr()
    asked.clear()
    with pytest.raises(SystemExit) as appless:
        shadow_report.main(["--gate", "tests"], {"AGENTIHOOKS_LAB_APP": ""}, fetch)
    assert appless.value.code == 2
    assert capsys.readouterr().err.endswith("error: --app or AGENTIHOOKS_LAB_APP names the lab App\n")
    assert asked == []


def window_of(event: str, lab_minute: int) -> dict[str, dict[str, Any]]:
    runs = f"repos/{REPOSITORY}/actions/workflows/test.yml/runs?event={event}&status=completed"
    found = [job("lint", minute=40), lab("lint", minute=lab_minute), waiting(0)]
    return {
        f"{runs}&per_page=100&page=1": {"workflow_runs": [{"head_sha": HEAD}]},
        f"repos/{REPOSITORY}/commits/{HEAD}/check-runs?filter=latest&per_page=100&page=1": {"check_runs": found},
    }


def lint_is_static(root: Path) -> frozenset[str]:
    return frozenset({"lint"})


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_the_pull_request_command_fails_a_window_whose_verdicts_are_first_yet_late_for_the_wait(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shadow, "static_jobs", lint_is_static)
    argv = ["--gate", "lint", "--heads", "1", "--app", APP]
    environ = {"GITHUB_REPOSITORY": REPOSITORY}
    assert shadow_report.main(argv, environ, window_of("pull_request", 21).__getitem__) == 1
    late = capsys.readouterr().out
    assert "aaaaaaaaaaaa  pass     pass     first  match      1260\n" in late
    assert late.endswith("lab verdict first 1 of at least 1\nlab verdict inside the wait 0 of at least 1\nrule fails\n")
    assert shadow_report.main(argv, environ, window_of("pull_request", 20).__getitem__) == 0
    assert capsys.readouterr().out.endswith(
        "lab verdict first 1 of at least 1\nlab verdict inside the wait 1 of at least 1\nrule holds\n"
    )
    merged = [*argv, "--event", "merge_group"]
    assert shadow_report.main(merged, environ, window_of("merge_group", 21).__getitem__) == 0
    unwaited = capsys.readouterr().out
    assert "aaaaaaaaaaaa  pass     pass     first  match\n" in unwaited
    assert unwaited.endswith("lab verdict first 1 of at least 1\nrule holds\n")


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_only_a_report_with_a_gate_is_the_acceptance_rule_report() -> None:
    assert shadow_report.claims(["report", "--gate", "lint"]) is True
    assert shadow_report.claims(["report", "--heads", "5", "--gate=lint"]) is True
    assert shadow_report.claims(["report", "--since", "g1"]) is False
    assert shadow_report.claims(["--gate", "lint"]) is False
    assert shadow_report.claims(["report", "--gateway", "lint"]) is False
    assert shadow_report.claims([]) is False


@pytest.mark.feature("lab-shadow-acceptance-report")
def test_a_paged_list_stops_at_a_short_page_or_after_ten_pages() -> None:
    asked: list[str] = []

    def full(path: str) -> dict[str, Any]:
        asked.append(path)
        return {"items": [{"page": len(asked)}] * 100}

    found = list(shadow_report.pages(full, "list?a=1", "items"))
    assert len(found) == 1000
    assert asked == [f"list?a=1&per_page=100&page={page}" for page in range(1, 11)]
    assert found[-1] == {"page": 10}
    asked.clear()

    def short(path: str) -> dict[str, Any]:
        asked.append(path)
        return {"items": [{}] * 99}

    assert len(list(shadow_report.pages(short, "list?a=1", "items"))) == 99
    assert asked == ["list?a=1&per_page=100&page=1"]


@pytest.mark.feature("lab-shadow-reusable-caller-pairing")
@pytest.mark.parametrize(
    ("failed_child", "verdict"),
    [(None, "pass"), ("scan", "fail"), ("feature-tests", "fail"), ("charts", "fail")],
)
def test_bare_semgrep_pairs_with_all_reusable_actions_jobs(failed_child: str | None, verdict: str) -> None:
    sources = shadow.Sources(shadow.static_jobs(shadow_report.ROOT), APP)
    runs = [
        job(f"semgrep / {child}", "failure" if child == failed_child else "success", minute)
        for child, minute in [("scan", 8), ("feature-tests", 12), ("charts", 10)]
    ]
    runs.append(lab("semgrep"))
    found = shadow_report.head(HEAD, runs, "semgrep", sources)
    assert found == shadow_report.Head(
        HEAD, shadow_report.Verdict(verdict, at(12)), shadow_report.Verdict("pass", at(5))
    )


@pytest.mark.feature("lab-shadow-reusable-caller-pairing")
@pytest.mark.parametrize("child", ["scan", "feature-tests", "charts"])
@pytest.mark.parametrize(("status", "conclusion"), [("in_progress", "success"), ("completed", "cancelled")])
def test_an_unfinished_reusable_child_excludes_the_semgrep_head(child: str, status: str, conclusion: str) -> None:
    sources = shadow.Sources(shadow.static_jobs(shadow_report.ROOT), APP)
    runs = [job(f"semgrep / {name}") for name in ["scan", "feature-tests", "charts"] if name != child]
    runs.extend([job(f"semgrep / {child}", conclusion, status=status), lab("semgrep")])
    assert shadow_report.head(HEAD, runs, "semgrep", sources) is None
