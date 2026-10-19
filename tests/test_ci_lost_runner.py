import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from scripts import ci_lost_runner

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
GRACE = timedelta(minutes=10)
NOW = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)
RUNNERS = {"anton-oss-light-live"}
RUN = {"id": 7, "run_attempt": 1, "status": "queued", "conclusion": None}
RUNS = "repos/o/r/actions/runs/7"
JOBS = ("--paginate", "repos/o/r/actions/runs/7/jobs?filter=latest&per_page=100", "--jq", ".jobs[]")
RUNNER_LIST = (
    "--paginate",
    "orgs/o/actions/runners?per_page=100",
    "--jq",
    '.runners[] | select(.status == "online") | .name',
)
OPEN_RUNS = "repos/o/r/actions/workflows/test.yml/runs?per_page=100&status="


def stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(minutes):
    return stamp(datetime.now(UTC) - timedelta(minutes=minutes))


def job(name, status="completed", conclusion="success", runner="anton-oss-light-live", started=None, group="anton-oss"):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "runner_name": runner,
        "runner_group_name": group,
        "started_at": started or stamp(NOW - timedelta(hours=1)),
    }


def lost(name="queue-fail-fast", started=None):
    return job(name, "in_progress", None, "anton-oss-light-gone", started or ago(60))


def gate():
    return job("Gate — Required", "queued", None, None)


def stalled_run():
    return [job("lint"), job("unit (3.12, 1)"), job("docs", conclusion="skipped"), lost(), gate()]


def lines(*items):
    return "".join(json.dumps(item) + "\n" for item in items)


class Api:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "-X":
            return ""
        value = self.pages[args if args[0] == "--paginate" else args[0]]
        return value.pop(0) if isinstance(value, list) else value

    def posts(self):
        return [call[1:] for call in self.calls if call[0] == "-X"]


def post(path):
    return ("POST", path)


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(ci_lost_runner.time, "sleep", slept.append)
    return slept


def test_the_gh_helper_is_the_queue_retry_one():
    from scripts import ci_queue_retry

    assert ci_lost_runner._api is ci_queue_retry._api


def test_live_runners_reads_every_page_of_online_organization_runners(monkeypatch):
    api = Api({RUNNER_LIST: "anton-a\nanton-b\n"})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.live_runners("o") == {"anton-a", "anton-b"}
    assert api.calls == [RUNNER_LIST]


def test_a_job_in_progress_on_an_absent_runner_past_the_grace_is_lost():
    jobs = [lost(started=stamp(NOW - GRACE)), job("lint")]
    assert ci_lost_runner.lost_jobs(jobs, RUNNERS, NOW, GRACE) == [jobs[0]]


@pytest.mark.parametrize(
    "candidate",
    [
        lost(started=stamp(NOW - GRACE + timedelta(seconds=1))),
        job("queue-fail-fast", "in_progress", None),
        job("queue-fail-fast", "in_progress", None, None),
        job("queue-fail-fast", "in_progress", None, ""),
        job("durations", "in_progress", None, "GitHub Actions 1000223300", group="GitHub Actions"),
        job("queue-fail-fast", "queued", None, "anton-oss-light-gone"),
        job("queue-fail-fast", "completed", "success", "anton-oss-light-gone"),
    ],
)
def test_a_young_live_unassigned_hosted_or_finished_job_is_not_lost(candidate):
    assert ci_lost_runner.lost_jobs([candidate], RUNNERS, NOW, GRACE) == []


def test_job_ids_map_base_names_to_workflow_keys():
    ids = ci_lost_runner.job_ids(WORKFLOW)
    assert ids["Gate — Required"] == "gate-required"
    assert ids["queue-fail-fast"] == "queue-fail-fast"
    assert ids["unit"] == "unit"
    assert ids["shard-check"] == "shard-check"


def test_a_run_waiting_only_on_its_lost_job_is_stalled():
    jobs = stalled_run()
    assert ci_lost_runner.stalled(jobs, [jobs[3]], WORKFLOW) == (True, "runner lost on queue-fail-fast")


def test_a_lost_unit_shard_blocks_its_matrix_judges():
    jobs = [lost("unit (3.12, 1)"), lost("unit (3.11, 2)"), job("shard-check (3.12)", "queued", None, None), gate()]
    assert ci_lost_runner.stalled(jobs, jobs[:2], WORKFLOW) == (True, "runner lost on unit (3.12, 1), unit (3.11, 2)")


def test_a_run_without_a_lost_job_is_not_stalled():
    assert ci_lost_runner.stalled(stalled_run(), [], WORKFLOW) == (False, "no job lost its runner")


def test_a_sibling_on_a_live_runner_keeps_the_run_going():
    jobs = [*stalled_run(), job("unit (3.12, 2)", "in_progress", None)]
    assert ci_lost_runner.stalled(jobs, [jobs[3]], WORKFLOW) == (False, "unit (3.12, 2) still runs on a live runner")


@pytest.mark.parametrize("status", ["queued", "waiting", "pending", "requested"])
def test_an_unfinished_job_that_does_not_need_the_lost_job_keeps_the_run_going(status):
    jobs = [*stalled_run(), job("unit (3.12, 3)", status, None, None)]
    assert ci_lost_runner.stalled(jobs, [jobs[3]], WORKFLOW) == (False, "unit (3.12, 3) still waits for a runner")


def test_a_lost_job_missing_from_the_workflow_blocks_no_queued_job():
    jobs = [lost("renamed-job"), gate()]
    assert ci_lost_runner.stalled(jobs, [jobs[0]], WORKFLOW) == (False, "Gate — Required still waits for a runner")


def test_a_first_attempt_that_only_lost_its_runner_is_rerun():
    assert ci_lost_runner.decide(RUN, stalled_run()) == ("rerun", "first attempt lost its runner")


@pytest.mark.parametrize("conclusion", ["failure", "timed_out"])
def test_a_run_that_failed_on_its_own_is_never_rerun(conclusion):
    jobs = [*stalled_run(), job("unit (3.12, 4)", conclusion=conclusion), job("lint (2)", conclusion="failure")]
    assert ci_lost_runner.decide(RUN, jobs) == ("cancel", "failed on its own: unit (3.12, 4), lint (2)")


def test_a_lost_job_blocks_a_matrix_dependent_by_its_base_name():
    workflow = {"jobs": {"build": {}, "judge": {"name": "judge (${{ matrix.part }})", "needs": "build"}}}
    jobs = [lost("build"), job("judge (1)", "queued", None, None)]
    assert ci_lost_runner.stalled(jobs, jobs[:1], workflow) == (True, "runner lost on build")


def test_a_cancelled_sibling_is_not_a_failure_of_its_own():
    assert ci_lost_runner.decide(RUN, [*stalled_run(), job("mutation", conclusion="cancelled")])[0] == "rerun"


def test_a_second_loss_is_cancelled_only():
    second = {**RUN, "run_attempt": 2}
    assert ci_lost_runner.decide(second, stalled_run()) == ("cancel", "attempt 2 lost its runner again")


def test_stop_returns_once_a_cancel_ends_the_run(monkeypatch, no_sleep):
    api = Api({RUNS: [json.dumps(RUN), json.dumps({**RUN, "status": "completed"})]})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.stop("o/r", 7)["status"] == "completed"
    assert api.posts() == [post(f"{RUNS}/cancel")]
    assert no_sleep == [ci_lost_runner.POLL_SECONDS] * 2


def test_stop_force_cancels_when_a_cancel_does_not_end_the_run(monkeypatch, no_sleep):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 2)
    states = [json.dumps(RUN)] * 3 + [json.dumps({**RUN, "status": "completed"})]
    api = Api({RUNS: states})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.stop("o/r", 7)["status"] == "completed"
    assert api.posts() == [post(f"{RUNS}/cancel"), post(f"{RUNS}/force-cancel")]
    assert len(no_sleep) == 4


def test_stop_returns_the_last_state_when_nothing_ends_the_run(monkeypatch, no_sleep):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 1)
    api = Api({RUNS: [json.dumps(RUN), json.dumps({**RUN, "status": "in_progress"})]})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.stop("o/r", 7)["status"] == "in_progress"
    assert api.posts() == [post(f"{RUNS}/cancel"), post(f"{RUNS}/force-cancel")]


def listing(**found):
    statuses = ("in_progress", "queued", "pending", "waiting", "requested")
    return {
        ("--paginate", f"{OPEN_RUNS}{status}", "--jq", ".workflow_runs[]"): found.get(status, "") for status in statuses
    }


def test_open_runs_lists_every_page_of_every_unfinished_status(monkeypatch):
    found = listing(
        in_progress=lines({"id": 1}),
        queued=lines({"id": 2}, {"id": 3}),
        pending=lines({"id": 4}),
        waiting=lines({"id": 5}),
        requested=lines({"id": 6}),
    )
    monkeypatch.setattr(ci_lost_runner, "_api", Api(found))
    assert ci_lost_runner.open_runs("o/r", "test.yml") == [{"id": n} for n in range(1, 7)]


def test_jobs_of_reads_every_page_of_the_latest_jobs(monkeypatch):
    monkeypatch.setattr(ci_lost_runner, "_api", Api({JOBS: lines({"name": "lint"}, {"name": "docs"})}))
    assert ci_lost_runner.jobs_of("o/r", 7) == [{"name": "lint"}, {"name": "docs"}]


def watched(monkeypatch, run, jobs, states):
    api = Api(
        {JOBS: lines(*jobs), RUNS: [json.dumps(state) for state in states], RUNNER_LIST: "anton-oss-light-live\n"}
    )
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    return api, ci_lost_runner.watch("o/r", run, WORKFLOW, GRACE)


def test_a_stalled_first_attempt_is_cancelled_then_rerun_once(monkeypatch, no_sleep):
    api, report = watched(monkeypatch, RUN, stalled_run(), [RUN, {**RUN, "status": "completed"}])
    assert report == {
        "run": 7,
        "attempt": 1,
        "action": "rerun",
        "reason": "runner lost on queue-fail-fast; first attempt lost its runner",
    }
    assert api.posts() == [post(f"{RUNS}/cancel"), post(f"{RUNS}/rerun-failed-jobs")]
    assert api.calls.count(RUNNER_LIST) == 1


def test_a_second_loss_is_cancelled_and_files_nothing(monkeypatch, no_sleep):
    second = {**RUN, "run_attempt": 2}
    api, report = watched(monkeypatch, second, stalled_run(), [second, {**second, "status": "completed"}])
    assert report == {
        "run": 7,
        "attempt": 2,
        "action": "cancel",
        "reason": "runner lost on queue-fail-fast; attempt 2 lost its runner again",
    }
    assert api.posts() == [post(f"{RUNS}/cancel")]


def test_a_stalled_run_with_a_real_test_failure_is_cancelled_and_never_rerun(monkeypatch, no_sleep):
    jobs = [*stalled_run(), job("unit (3.12, 4)", conclusion="failure")]
    api, report = watched(monkeypatch, RUN, jobs, [RUN, {**RUN, "status": "completed"}])
    assert report == {
        "run": 7,
        "attempt": 1,
        "action": "cancel",
        "reason": "runner lost on queue-fail-fast; failed on its own: unit (3.12, 4)",
    }
    assert api.posts() == [post(f"{RUNS}/cancel")]


def test_a_run_that_will_not_end_is_reported_and_not_rerun(monkeypatch, no_sleep):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 1)
    api, report = watched(monkeypatch, RUN, stalled_run(), [RUN, RUN, RUN])
    assert report == {
        "run": 7,
        "attempt": 1,
        "action": "cancel",
        "reason": "runner lost on queue-fail-fast; the run did not end after force cancel",
    }
    assert api.posts() == [post(f"{RUNS}/cancel"), post(f"{RUNS}/force-cancel")]


def test_a_run_rerun_meanwhile_is_left_alone(monkeypatch, no_sleep):
    api, report = watched(monkeypatch, RUN, stalled_run(), [{**RUN, "run_attempt": 2}])
    assert report == {"run": 7, "attempt": 1, "action": "none", "reason": "the run was rerun meanwhile"}
    assert api.posts() == []


def refused_cancel(monkeypatch, after):
    api = Api({JOBS: lines(*stalled_run()), RUNS: [json.dumps(RUN), json.dumps(after)], RUNNER_LIST: ""})

    def refusing(*args):
        if args == ("-X", "POST", f"{RUNS}/cancel"):
            raise subprocess.CalledProcessError(1, ["gh", "api", *args])
        return api(*args)

    monkeypatch.setattr(ci_lost_runner, "_api", refusing)
    return api


def test_a_cancel_whose_answer_was_lost_still_reruns_the_cancelled_run(monkeypatch, no_sleep):
    api = refused_cancel(monkeypatch, {**RUN, "status": "completed", "conclusion": "cancelled"})
    report = ci_lost_runner.watch("o/r", RUN, WORKFLOW, GRACE)
    assert report["action"] == "rerun"
    assert api.posts() == [post(f"{RUNS}/rerun-failed-jobs")]


def test_a_cancel_refused_because_the_run_completed_is_left_alone(monkeypatch, no_sleep):
    refused_cancel(monkeypatch, {**RUN, "status": "completed"})
    report = ci_lost_runner.watch("o/r", RUN, WORKFLOW, GRACE)
    assert report == {"run": 7, "attempt": 1, "action": "none", "reason": "the run completed meanwhile"}


def test_a_cancel_refused_on_an_open_run_is_raised(monkeypatch, no_sleep):
    refused_cancel(monkeypatch, RUN)
    with pytest.raises(subprocess.CalledProcessError):
        ci_lost_runner.watch("o/r", RUN, WORKFLOW, GRACE)


def test_a_run_still_working_is_left_alone(monkeypatch, no_sleep):
    jobs = [*stalled_run(), job("unit (3.12, 2)", "in_progress", None)]
    api, report = watched(monkeypatch, RUN, jobs, [])
    assert report == {"run": 7, "attempt": 1, "action": "none", "reason": "unit (3.12, 2) still runs on a live runner"}
    assert api.posts() == []


def test_a_lost_job_inside_its_grace_is_left_alone(monkeypatch, no_sleep):
    api, report = watched(monkeypatch, RUN, [job("lint"), lost(started=ago(9)), gate()], [])
    assert report["reason"] == "no job lost its runner"
    assert api.posts() == []


@pytest.mark.parametrize("conclusion", ["failure", "success", "cancelled"])
def test_a_completed_run_is_left_alone_at_first_look(monkeypatch, conclusion):
    done = {**RUN, "status": "completed", "conclusion": conclusion}
    api = Api({})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    report = ci_lost_runner.first_look("o/r", done, WORKFLOW, GRACE)
    assert report == {"run": 7, "attempt": 1, "action": "none", "reason": f"run completed with conclusion {conclusion}"}
    assert api.calls == []


@pytest.mark.parametrize("status", ["in_progress", "queued", "pending", "waiting", "requested"])
def test_first_look_names_an_unfinished_suspect_by_returning_nothing(monkeypatch, status):
    monkeypatch.setattr(ci_lost_runner, "_api", Api({JOBS: lines(*stalled_run()), RUNNER_LIST: ""}))
    assert ci_lost_runner.first_look("o/r", {**RUN, "status": status}, WORKFLOW, GRACE) is None


def test_first_look_reports_a_healthy_open_run(monkeypatch):
    monkeypatch.setattr(ci_lost_runner, "_api", Api({JOBS: lines(job("lint")), RUNNER_LIST: ""}))
    report = ci_lost_runner.first_look("o/r", RUN, WORKFLOW, GRACE)
    assert report == {"run": 7, "attempt": 1, "action": "none", "reason": "no job lost its runner"}


def named_runs(monkeypatch, confirm):
    done = {**RUN, "id": 8, "status": "completed", "conclusion": "failure"}
    pages = {
        RUNS: [json.dumps(RUN), json.dumps(RUN), json.dumps({**RUN, "status": "completed"})],
        "repos/o/r/actions/runs/8": json.dumps(done),
        JOBS: [lines(*stalled_run()), lines(*confirm)],
        RUNNER_LIST: "anton-oss-light-live\n",
    }
    monkeypatch.setattr(ci_lost_runner, "_api", Api(pages))
    monkeypatch.chdir(ROOT)


def test_main_confirms_every_suspect_after_one_wait_then_acts(monkeypatch, capsys, no_sleep):
    named_runs(monkeypatch, stalled_run())
    assert ci_lost_runner.main(["--repo", "o/r", "--run-id", "7", "--run-id", "8"]) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(report["run"], report["action"]) for report in reports] == [(8, "none"), (7, "rerun")]
    assert reports[0]["reason"] == "run completed with conclusion failure"
    assert no_sleep.count(ci_lost_runner.CONFIRM_SECONDS) == 1


def test_main_leaves_a_loss_that_clears_on_the_second_read(monkeypatch, capsys, no_sleep):
    named_runs(monkeypatch, [*stalled_run()[:3], job("queue-fail-fast", "in_progress", None), gate()])
    assert ci_lost_runner.main(["--repo", "o/r", "--run-id", "7", "--run-id", "8"]) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[1] == {
        "run": 7,
        "attempt": 1,
        "action": "none",
        "reason": "no job lost its runner",
    }


def test_main_scans_open_runs_with_its_grace_and_waits_only_for_suspects(monkeypatch, capsys, no_sleep):
    pages = {
        **listing(queued=lines(RUN)),
        JOBS: lines(job("lint"), lost(started=ago(11)), gate()),
        RUNNER_LIST: "",
    }
    monkeypatch.setattr(ci_lost_runner, "_api", Api(pages))
    monkeypatch.chdir(ROOT)
    assert ci_lost_runner.main(["--repo", "o/r", "--workflow", "test.yml", "--grace-minutes", "11.5"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "no job lost its runner"
    assert no_sleep == []


def test_main_reports_a_refused_call_and_keeps_watching(monkeypatch, capsys, no_sleep):
    done = {**RUN, "id": 8, "status": "completed", "conclusion": "success"}
    api = Api({RUNS: json.dumps(RUN), "repos/o/r/actions/runs/8": json.dumps(done)})

    def refusing(*args):
        if args == JOBS:
            raise subprocess.CalledProcessError(1, ["gh", "api", "jobs"])
        return api(*args)

    monkeypatch.setattr(ci_lost_runner, "_api", refusing)
    monkeypatch.chdir(ROOT)
    assert ci_lost_runner.main(["--repo", "o/r", "--run-id", "7", "--run-id", "8"]) == 1
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[0] == {
        "run": 7,
        "attempt": 1,
        "action": "error",
        "reason": "Command '['gh', 'api', 'jobs']' returned non-zero exit status 1.",
    }
    assert reports[1]["action"] == "none"


def test_main_needs_a_repository():
    with pytest.raises(SystemExit):
        ci_lost_runner.main([])


def test_the_watchdog_workflow_runs_the_script_every_ten_minutes_with_the_app_token():
    workflow = yaml.safe_load((ROOT / ".github/workflows/lost-runner.yml").read_text())
    assert workflow[True]["schedule"] == [{"cron": "*/10 * * * *"}]
    steps = workflow["jobs"]["watch"]["steps"]
    mint = next(step for step in steps if step.get("id") == "app-token")
    assert mint["with"]["permission-actions"] == "write"
    assert mint["with"]["permission-organization-self-hosted-runners"] == "read"
    assert steps[-1]["env"] == {"GH_TOKEN": "${{ steps.app-token.outputs.token }}", "PYTHONUNBUFFERED": "1"}
    assert steps[-1]["run"] == 'python -m scripts.ci_lost_runner --repo "$GITHUB_REPOSITORY"'
