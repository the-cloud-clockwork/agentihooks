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


def lost(name="Gate — Required", started=None):
    return job(name, "in_progress", None, "anton-oss-light-gone", started or ago(60))


def gate():
    return job("Queue gate summary", "queued", None, None)


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


def test_a_job_whose_every_step_completed_on_an_absent_runner_is_not_lost():
    candidate = {**lost("unit (3.12, 2)", stamp(NOW - GRACE)), "steps": [{"status": "completed"}] * 2}
    assert ci_lost_runner.lost_jobs([candidate], RUNNERS, NOW, GRACE) == []


@pytest.mark.parametrize(
    "steps",
    [None, [], [{"status": "completed"}, {"status": "in_progress"}], [{"status": "completed"}, {"status": "queued"}]],
)
def test_a_job_with_no_steps_or_an_unfinished_step_on_an_absent_runner_is_lost(steps):
    candidate = lost("unit (3.12, 2)", stamp(NOW - GRACE))
    if steps is not None:
        candidate["steps"] = steps
    assert ci_lost_runner.lost_jobs([candidate], RUNNERS, NOW, GRACE) == [candidate]


def test_job_ids_map_base_names_to_workflow_keys():
    from scripts import ci_gate_queue

    ids = ci_lost_runner.job_ids(WORKFLOW)
    if WORKFLOW["jobs"]["gate-required"]["name"] == "Gate — Required":
        assert ids["Gate — Required"] == "gate-required"
        assert ids["queue-fail-fast"] == "queue-fail-fast"
    else:
        assert WORKFLOW["jobs"]["gate-required"]["name"] == ci_gate_queue.SUMMARY_NAME
        assert WORKFLOW["jobs"]["queue-fail-fast"]["name"] == ci_gate_queue.GATE_NAME
        assert ids["Gate — Required"] == "queue-fail-fast"
        assert ids["Queue gate summary"] == "gate-required"
    assert ids["unit"] == "unit"
    assert ids["shard-check"] == "shard-check"


def test_a_run_waiting_only_on_its_lost_job_is_stalled():
    jobs = stalled_run()
    assert ci_lost_runner.stalled(jobs, [jobs[3]], WORKFLOW) == (True, "runner lost on Gate — Required")


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
    assert ci_lost_runner.stalled(jobs, [jobs[0]], WORKFLOW) == (False, "Queue gate summary still waits for a runner")


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
    assert ci_lost_runner.stop("o/r", 7, {})["status"] == "completed"
    assert api.posts() == [post(f"{RUNS}/cancel")]
    assert no_sleep == [ci_lost_runner.POLL_SECONDS] * 2


def test_stop_force_cancels_when_a_cancel_does_not_end_the_run(monkeypatch, no_sleep):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 2)
    states = [json.dumps(RUN)] * 3 + [json.dumps({**RUN, "status": "completed"})]
    api = Api({RUNS: states})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.stop("o/r", 7, {})["status"] == "completed"
    assert api.posts() == [post(f"{RUNS}/cancel"), post(f"{RUNS}/force-cancel")]
    assert len(no_sleep) == 4


def test_stop_returns_the_last_state_when_nothing_ends_the_run(monkeypatch, no_sleep):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 1)
    api = Api({RUNS: [json.dumps(RUN), json.dumps({**RUN, "status": "in_progress"})]})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.stop("o/r", 7, {})["status"] == "in_progress"
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


@pytest.mark.parametrize("force,completed", [(False, True), (True, True), (True, False)])
def test_recovery_records_each_cancel_and_observed_completion(monkeypatch, no_sleep, force, completed):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 1)
    moments = iter(NOW + timedelta(seconds=i) for i in range(5))

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            assert tz is UTC
            return next(moments)

    monkeypatch.setattr(ci_lost_runner, "datetime", Clock)
    states = [RUN]
    if force:
        states.append(RUN)
    states.append({**RUN, "status": "completed"} if completed else RUN)
    api, record = watched(monkeypatch, RUN, [lost(started=stamp(NOW - timedelta(hours=1)))], states)
    assert record["detected_at"] == "2026-10-19T16:00:01+00:00"
    assert record["cancel_requested_at"] == "2026-10-19T16:00:02+00:00"
    assert record["force_cancel_requested_at"] == ("2026-10-19T16:00:03+00:00" if force else None)
    assert record["completed_observed_at"] == (f"2026-10-19T16:00:0{4 if force else 3}+00:00" if completed else None)
    assert record["action"] == ("rerun" if completed else "cancel")
    assert api.posts()[0] == post(f"{RUNS}/cancel")


def test_a_stalled_first_attempt_is_cancelled_then_rerun_once(monkeypatch, no_sleep):
    api, report = watched(monkeypatch, RUN, stalled_run(), [RUN, {**RUN, "status": "completed"}])
    assert {key: report[key] for key in ("run", "attempt", "action", "reason")} == {
        "run": 7,
        "attempt": 1,
        "action": "rerun",
        "reason": "runner lost on Gate — Required; first attempt lost its runner",
    }
    assert api.posts() == [post(f"{RUNS}/cancel"), post(f"{RUNS}/rerun-failed-jobs")]
    assert api.calls.count(RUNNER_LIST) == 1


def test_a_second_loss_is_cancelled_and_files_nothing(monkeypatch, no_sleep):
    second = {**RUN, "run_attempt": 2}
    api, report = watched(monkeypatch, second, stalled_run(), [second, {**second, "status": "completed"}])
    assert {key: report[key] for key in ("run", "attempt", "action", "reason")} == {
        "run": 7,
        "attempt": 2,
        "action": "cancel",
        "reason": "runner lost on Gate — Required; attempt 2 lost its runner again",
    }
    assert api.posts() == [post(f"{RUNS}/cancel")]


def test_a_stalled_run_with_a_real_test_failure_is_cancelled_and_never_rerun(monkeypatch, no_sleep):
    jobs = [*stalled_run(), job("unit (3.12, 4)", conclusion="failure")]
    api, report = watched(monkeypatch, RUN, jobs, [RUN, {**RUN, "status": "completed"}])
    assert {key: report[key] for key in ("run", "attempt", "action", "reason")} == {
        "run": 7,
        "attempt": 1,
        "action": "cancel",
        "reason": "runner lost on Gate — Required; failed on its own: unit (3.12, 4)",
    }
    assert api.posts() == [post(f"{RUNS}/cancel")]


def test_a_run_that_will_not_end_is_reported_and_not_rerun(monkeypatch, no_sleep):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 1)
    api, report = watched(monkeypatch, RUN, stalled_run(), [RUN, RUN, RUN])
    assert {key: report[key] for key in ("run", "attempt", "action", "reason")} == {
        "run": 7,
        "attempt": 1,
        "action": "cancel",
        "reason": "runner lost on Gate — Required; the run did not end after force cancel",
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
    assert {key: report[key] for key in ("run", "attempt", "action", "reason")} == {
        "run": 7,
        "attempt": 1,
        "action": "none",
        "reason": "the run completed meanwhile",
    }


@pytest.mark.parametrize("conclusion,action", [("cancelled", "rerun"), ("success", "none")])
def test_a_cancel_error_records_completion_seen_on_the_followup_read(monkeypatch, no_sleep, conclusion, action):
    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            assert tz is UTC
            return datetime.now(tz)

    monkeypatch.setattr(ci_lost_runner, "datetime", Clock)
    refused_cancel(monkeypatch, {**RUN, "status": "completed", "conclusion": conclusion})
    record = ci_lost_runner.guarded(ci_lost_runner.watch, "o/r", RUN, WORKFLOW, GRACE)
    assert record["action"] == action
    assert set(record) == {
        "run",
        "attempt",
        "action",
        "reason",
        "detected_at",
        "cancel_requested_at",
        "force_cancel_requested_at",
        "completed_observed_at",
    }
    assert record["force_cancel_requested_at"] is None
    completed = datetime.fromisoformat(record["completed_observed_at"])
    assert completed.utcoffset() == timedelta(0)
    assert datetime.fromisoformat(record["cancel_requested_at"]) <= completed


def test_a_cancel_refused_on_an_open_run_keeps_its_timestamps(monkeypatch, no_sleep):
    refused_cancel(monkeypatch, RUN)
    record = ci_lost_runner.guarded(ci_lost_runner.watch, "o/r", RUN, WORKFLOW, GRACE)
    assert record["action"] == "error"
    assert record["run"] == 7
    assert record["attempt"] == 1
    assert "cancel" in record["reason"]
    assert datetime.fromisoformat(record["detected_at"]) <= datetime.fromisoformat(record["cancel_requested_at"])
    assert record["force_cancel_requested_at"] is None
    assert record["completed_observed_at"] is None


@pytest.mark.parametrize("failed_action", ["force-cancel", "rerun-failed-jobs", "read"])
def test_recovery_errors_keep_the_collected_timestamps(monkeypatch, no_sleep, failed_action):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 1)
    states = [json.dumps(RUN), json.dumps(RUN), json.dumps(RUN)]
    if failed_action == "rerun-failed-jobs":
        states[1] = json.dumps({**RUN, "status": "completed"})
    api = Api({JOBS: lines(*stalled_run()), RUNS: states, RUNNER_LIST: ""})

    def failing(*args):
        if args == ("-X", "POST", f"{RUNS}/{failed_action}") or (
            failed_action == "read" and args == (RUNS,) and api.posts()
        ):
            raise subprocess.CalledProcessError(1, ["gh", "api", *args])
        return api(*args)

    monkeypatch.setattr(ci_lost_runner, "_api", failing)
    record = ci_lost_runner.guarded(ci_lost_runner.watch, "o/r", RUN, WORKFLOW, GRACE)
    assert record["action"] == "error"
    assert record["run"] == 7
    assert record["attempt"] == 1
    assert (failed_action if failed_action != "read" else RUNS) in record["reason"]
    assert datetime.fromisoformat(record["detected_at"]) <= datetime.fromisoformat(record["cancel_requested_at"])
    assert (record["force_cancel_requested_at"] is not None) == (failed_action == "force-cancel")
    assert (record["completed_observed_at"] is not None) == (failed_action == "rerun-failed-jobs")


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
    named_runs(monkeypatch, [*stalled_run()[:3], job("Gate — Required", "in_progress", None), gate()])
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


CLUSTER = json.loads((ROOT / "tests/fixtures/lost_runner_cluster.json").read_text())
LOSS = datetime(2026, 10, 19, 17, 13, 27, tzinfo=UTC)
BOUND = timedelta(seconds=ci_lost_runner.NODE_BOUND_SECONDS)


def cluster(pods=None, nodes=None, now=LOSS, sets=None):
    return ci_lost_runner.ClusterRunners(
        set(CLUSTER["sets"]) if sets is None else sets,
        CLUSTER["pods"] if pods is None else pods,
        CLUSTER["nodes"] if nodes is None else nodes,
        now,
        BOUND,
    )


def test_a_runner_pod_running_on_a_ready_node_is_live():
    assert "anton-oss-light-cpwxt-runner-live1" in cluster()


def test_a_runner_pod_that_ended_without_reporting_is_lost_at_once():
    assert "anton-oss-light-cpwxt-runner-clfn7" not in cluster()


def test_a_runner_pod_on_a_node_not_ready_past_the_bound_is_lost():
    assert "anton-oss-light-cpwxt-runner-down1" not in cluster()


def test_a_runner_pod_on_a_node_not_ready_inside_the_bound_is_live():
    since = datetime.fromisoformat(CLUSTER["nodes"][0]["status"]["conditions"][0]["lastTransitionTime"])
    assert "anton-oss-light-cpwxt-runner-down1" in cluster(now=since + BOUND - timedelta(seconds=1))


def test_a_runner_pod_in_phase_unknown_on_a_node_not_ready_inside_the_bound_is_live():
    since = datetime.fromisoformat(CLUSTER["nodes"][0]["status"]["conditions"][0]["lastTransitionTime"])
    pod = json.loads(json.dumps(CLUSTER["pods"][2]))
    pod["status"]["phase"] = "Unknown"
    assert "anton-oss-light-cpwxt-runner-down1" in cluster(pods=[pod], now=since + BOUND - timedelta(seconds=1))
    assert "anton-oss-light-cpwxt-runner-down1" not in cluster(pods=[pod], now=since + BOUND)


def test_a_runner_pod_in_phase_unknown_on_a_ready_node_is_lost():
    pod = json.loads(json.dumps(CLUSTER["pods"][0]))
    pod["status"]["phase"] = "Unknown"
    assert "anton-oss-light-cpwxt-runner-live1" not in cluster(pods=[pod])


def test_a_runner_container_that_terminated_in_a_running_pod_is_lost():
    pod = json.loads(json.dumps(CLUSTER["pods"][0]))
    pod["status"]["containerStatuses"][0]["state"] = CLUSTER["pods"][1]["status"]["containerStatuses"][0]["state"]
    assert "anton-oss-light-cpwxt-runner-live1" not in cluster(pods=[pod])


@pytest.mark.parametrize("phase", ["Failed", "Succeeded"])
def test_a_runner_pod_whose_phase_ended_is_lost_while_its_container_still_reads_running(phase):
    pod = json.loads(json.dumps(CLUSTER["pods"][0]))
    pod["status"]["phase"] = phase
    assert "anton-oss-light-cpwxt-runner-live1" not in cluster(pods=[pod])


def test_a_scale_set_whose_name_holds_the_runner_word_is_still_matched():
    assert "arc-runner-pool-abcde-runner-x1y2z" not in cluster(pods=[], sets={"arc-runner-pool"})


def test_a_runner_pod_missing_from_a_visible_scale_set_is_lost():
    assert "anton-oss-light-cpwxt-runner-gone1" not in cluster()


@pytest.mark.parametrize("name", ["anton-gpu-5d778-runner-x1y2z", "workstation", "GitHub Actions 1000223407"])
def test_a_runner_the_cluster_cannot_see_is_never_counted_lost(name):
    assert name in cluster()


def test_no_runner_sets_means_no_loss():
    assert "anton-oss-light-cpwxt-runner-gone1" in cluster(sets=set())


def test_a_runner_of_a_set_with_no_pods_left_is_lost():
    assert "anton-oss-queue-x7k2p-runner-gone1" not in cluster(pods=[])


def test_a_runner_pod_on_a_node_deleted_from_the_cluster_is_lost():
    assert "anton-oss-light-cpwxt-runner-live1" not in cluster(nodes=[])


def test_a_pending_pod_without_a_node_is_live():
    pod = json.loads(json.dumps(CLUSTER["pods"][0]))
    pod["spec"] = {}
    pod["status"] = {"phase": "Pending"}
    assert "anton-oss-light-cpwxt-runner-live1" in cluster(pods=[pod], nodes=[])


def test_a_node_without_a_ready_condition_is_not_down():
    node = {"metadata": {"name": "anton-oci-vjnhihp4znsq"}, "status": {}}
    assert "anton-oss-light-cpwxt-runner-live1" in cluster(nodes=[node])


def test_a_pod_ended_on_a_ready_node_makes_its_recorded_job_lost_with_no_grace():
    jobs = [CLUSTER["job"], job("lint")]
    assert ci_lost_runner.lost_jobs(jobs, cluster(), LOSS, timedelta(0)) == [CLUSTER["job"]]


def test_cluster_runners_reads_runner_sets_labelled_pods_and_nodes(monkeypatch):
    calls = []
    sets = [{"metadata": {"name": name}} for name in CLUSTER["sets"]]

    def run(command, **kwargs):
        calls.append((command, kwargs))
        items = {"autoscalingrunnersets": sets, "pods": CLUSTER["pods"], "nodes": CLUSTER["nodes"]}[command[2]]
        return subprocess.CompletedProcess(command, 0, json.dumps({"items": items}), "")

    monkeypatch.setattr(ci_lost_runner.subprocess, "run", run)
    runners = ci_lost_runner.cluster_runners("o")
    assert [command for command, _ in calls] == [
        ["kubectl", "get", "autoscalingrunnersets", "--all-namespaces", "-o", "json"],
        ["kubectl", "get", "pods", "--all-namespaces", "-l", ci_lost_runner.SCALE_SET, "-o", "json"],
        ["kubectl", "get", "nodes", "-o", "json"],
    ]
    assert all(kwargs == {"check": True, "capture_output": True, "timeout": 60} for _, kwargs in calls)
    assert runners.sets == set(CLUSTER["sets"])
    assert runners.live == {"anton-oss-light-cpwxt-runner-live1"}


@pytest.mark.parametrize(
    "failure",
    [subprocess.CalledProcessError(1, ["kubectl"]), subprocess.TimeoutExpired(["kubectl"], 60)],
)
def test_a_refused_or_timed_out_cluster_read_is_an_error_never_a_loss(monkeypatch, capsys, no_sleep, failure):
    def refused(command, **kwargs):
        raise failure

    api = Api({RUNS: json.dumps(RUN), JOBS: lines(CLUSTER["job"], gate())})
    monkeypatch.setattr(ci_lost_runner.subprocess, "run", refused)
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    monkeypatch.chdir(ROOT)
    assert ci_lost_runner.main(["--repo", "o/r", "--run-id", "7", "--cluster"]) == 1
    assert json.loads(capsys.readouterr().out)["action"] == "error"
    assert api.posts() == []


def test_main_in_cluster_mode_acts_on_a_fresh_loss_after_a_short_confirmation(monkeypatch, capsys, no_sleep):
    pages = {
        RUNS: [json.dumps(RUN), json.dumps(RUN), json.dumps({**RUN, "status": "completed"})],
        JOBS: [lines({**CLUSTER["job"], "name": "Gate — Required", "started_at": ago(0.1)}, gate())] * 2,
    }
    api = Api(pages)
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    monkeypatch.setattr(ci_lost_runner, "cluster_runners", lambda _org: cluster(now=datetime.now(UTC)))
    monkeypatch.chdir(ROOT)
    assert ci_lost_runner.main(["--repo", "o/r", "--run-id", "7", "--cluster"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "rerun"
    assert no_sleep[0] == ci_lost_runner.CLUSTER_CONFIRM_SECONDS
    assert RUNNER_LIST not in api.calls


def test_main_leaves_the_run_to_a_watchdog_that_holds_the_lock(monkeypatch, capsys, tmp_path):
    lock = tmp_path / "lost-runner.lock"
    held = ci_lost_runner._hold(str(lock))
    api = Api({})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.main(["--repo", "o/r", "--cluster", "--lock", str(lock)]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": "none", "reason": "another watchdog holds the lock"}
    assert api.calls == []
    held.close()
    again = ci_lost_runner._hold(str(lock))
    assert again is not None
    again.close()


def test_main_with_a_free_lock_holds_it_while_it_scans(monkeypatch, capsys, tmp_path):
    lock = tmp_path / "lost-runner.lock"
    seen = []
    api = Api(listing())

    def scan(*args):
        seen.append(ci_lost_runner._hold(str(lock)))
        return api(*args)

    monkeypatch.setattr(ci_lost_runner, "_api", scan)
    monkeypatch.chdir(ROOT)
    assert ci_lost_runner.main(["--repo", "o/r", "--cluster", "--lock", str(lock)]) == 0
    assert capsys.readouterr().out == ""
    assert len(api.calls) == 5
    assert seen == [None] * 5
