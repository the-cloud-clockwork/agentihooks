import json
import subprocess
import time

import pytest

from scripts.swarm import queue_exit
from scripts.swarm.store import SwarmError

URL = "https://github.com/o/r/pull/7"
SINCE = 1_792_350_000_000
PATH = "repos/o/r/actions/runs?event=merge_group&created=>=2026-10-18T19:00:00Z&per_page=100&page={page}"


COMMITTED = "2026-10-20T08:00:00Z"
FRESH = "repos/o/r/actions/runs?event=merge_group&created=>=2026-10-20T08:00:00Z&per_page=100&page=1"


def run_of(number, conclusion, name="Tests", run_id=1, group="g1", created="2026-10-20T09:00:00Z"):
    return {
        "id": run_id,
        "name": name,
        "conclusion": conclusion,
        "head_branch": f"gh-readonly-queue/dev/pr-{number}-{'b' * 40}",
        "head_sha": group,
        "created_at": created,
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


def jobs_path(run_id, page=1):
    return f"repos/o/r/actions/runs/{run_id}/jobs?per_page=100&page={page}"


def runner(*pages, total=None, code=0, jobs=None):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        path = command[2]
        if "/jobs?" in path:
            listed = (jobs or {}).get(int(path.split("/runs/")[1].split("/")[0]), [[]])
            page = listed[int(path.rsplit("page=", 1)[1]) - 1]
            body = {"total_count": sum(map(len, listed)), "jobs": [{"conclusion": c} for c in page]}
        elif "/commits/" in path:
            body = {"commit": {"committer": {"date": COMMITTED}}}
        else:
            page = pages[sum("actions/runs?" in call[2] for call in calls) - 1]
            body = {"total_count": sum(map(len, pages)) if total is None else total, "workflow_runs": page}
        return subprocess.CompletedProcess(command, code, stdout=json.dumps(body), stderr="boom" if code else "")

    run.calls = calls
    return run


@pytest.fixture
def tokyo(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_the_since_bound_is_utc_whatever_the_local_zone(tokyo):
    run = runner([])
    assert queue_exit.own_failures(URL, SINCE, run) == []
    assert run.calls == [["gh", "api", PATH.format(page=1)]]


def test_cancelled_and_passed_own_runs_are_no_failure():
    run = runner(
        [run_of(7, "cancelled"), run_of(7, "success", run_id=2), run_of(8, None, run_id=3)],
        jobs={1: [["success", "cancelled", "skipped", None]]},
    )
    assert queue_exit.own_failures(URL, SINCE, run) == []
    assert run.calls == [["gh", "api", PATH.format(page=1)], ["gh", "api", jobs_path(1)]]


@pytest.mark.parametrize("conclusion", queue_exit.FAILED_JOB)
def test_a_cancelled_own_run_with_a_failed_job_is_the_entrys_own_failure(conclusion):
    run = runner([run_of(7, "cancelled", run_id=5)], jobs={5: [["success", conclusion, "cancelled"]]})
    assert queue_exit.own_failures(URL, SINCE, run) == ["Tests https://github.com/o/r/actions/runs/5"]


def test_every_job_page_of_a_cancelled_run_is_read():
    run = runner([run_of(7, "cancelled", run_id=5)], jobs={5: [["success"] * 100, ["failure"]]})
    assert queue_exit.own_failures(URL, SINCE, run) == ["Tests https://github.com/o/r/actions/runs/5"]
    assert run.calls[-1] == ["gh", "api", jobs_path(5, page=2)]


def test_jobs_of_other_pull_requests_runs_are_not_read():
    run = runner([run_of(8, "cancelled", run_id=5)], jobs={5: [["failure"]]})
    assert queue_exit.own_failures(URL, SINCE, run) == []
    assert run.calls == [["gh", "api", PATH.format(page=1)]]


def test_an_own_run_still_running_holds_the_answer():
    run = runner([run_of(7, "cancelled"), run_of(7, None, run_id=3)])
    with pytest.raises(SwarmError, match="still running: https://github.com/o/r/actions/runs/3$"):
        queue_exit.own_failures(URL, SINCE, run)


def test_every_running_own_run_is_named():
    run = runner([run_of(7, None, run_id=3), run_of(7, None, run_id=4)])
    with pytest.raises(
        SwarmError, match="still running: https://github.com/o/r/actions/runs/3, https://github.com/o/r/actions/runs/4$"
    ):
        queue_exit.own_failures(URL, SINCE, run)


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
def test_a_failed_own_run_is_named(conclusion):
    run = runner([run_of(7, "cancelled"), run_of(7, conclusion, run_id=2)])
    assert queue_exit.own_failures(URL, SINCE, run) == ["Tests https://github.com/o/r/actions/runs/2"]


def test_failures_of_other_pull_requests_and_branches_are_ignored():
    other = run_of(8, "failure", run_id=2)
    prefixed = run_of(70, "failure", run_id=3)
    stray = {**run_of(7, "failure", run_id=4), "head_branch": "dev"}
    assert queue_exit.own_failures(URL, SINCE, runner([other, prefixed, stray])) == []


def test_every_page_is_read():
    run = runner([run_of(8, "success")] * 100, [run_of(7, "failure", run_id=2)])
    assert queue_exit.own_failures(URL, SINCE, run) == ["Tests https://github.com/o/r/actions/runs/2"]
    assert run.calls[1] == ["gh", "api", PATH.format(page=2)]


def test_the_last_capped_page_is_read():
    pages = [[run_of(8, "success")] * 100] * (queue_exit.PAGES - 1) + [[run_of(7, "failure", run_id=2)]]
    run = runner(*pages)
    assert queue_exit.own_failures(URL, SINCE, run) == ["Tests https://github.com/o/r/actions/runs/2"]
    assert run.calls[-1] == ["gh", "api", PATH.format(page=queue_exit.PAGES)]


def test_a_listing_that_stops_short_is_unknown():
    assert queue_exit.own_failures(URL, SINCE, runner([run_of(7, "cancelled")], [], total=5)) is None


def test_a_listing_past_the_page_cap_is_unknown():
    pages = [[run_of(8, "success")] * 100] * queue_exit.PAGES
    assert queue_exit.own_failures(URL, SINCE, runner(*pages, total=100 * queue_exit.PAGES + 1)) is None


def test_an_api_failure_raises():
    with pytest.raises(SwarmError, match="boom"):
        queue_exit.own_failures(URL, SINCE, runner([], code=1))


def test_a_head_with_no_group_run_since_it_landed_may_queue():
    run = runner([run_of(8, "failure"), run_of(7, None, run_id=2)])
    assert queue_exit.require_fresh(URL, "abc", run) is None
    assert run.calls == [["gh", "api", "repos/o/r/commits/abc"], ["gh", "api", FRESH]]


def test_a_head_whose_last_group_had_a_failed_job_is_refused_naming_the_run():
    older = run_of(7, "success", run_id=1, group="g0", created="2026-10-20T08:30:00Z")
    last = run_of(7, "cancelled", run_id=5, group="g1")
    other = run_of(7, "success", name="Proofs", run_id=6, group="g1")
    run = runner([older, last, other], jobs={5: [["success", "failure"]]})
    with pytest.raises(SwarmError) as refused:
        queue_exit.require_fresh(URL, "abc", run)
    assert str(refused.value) == (
        "head abc failed its last merge group run (Tests https://github.com/o/r/actions/runs/5); "
        "push a new commit before queueing again"
    )


def test_an_older_failed_group_does_not_refuse_a_clean_last_group():
    older = run_of(7, "failure", run_id=1, group="g0", created="2026-10-20T08:30:00Z")
    last = run_of(7, "cancelled", run_id=5, group="g1")
    run = runner([last, older], jobs={5: [["cancelled", "success"]]})
    assert queue_exit.require_fresh(URL, "abc", run) is None


def test_a_running_group_is_not_the_last_group():
    failed = run_of(7, "failure", run_id=1, group="g0", created="2026-10-20T08:30:00Z")
    running = run_of(7, None, run_id=2, group="g1")
    with pytest.raises(SwarmError, match=r"\(Tests https://github.com/o/r/actions/runs/1\)"):
        queue_exit.require_fresh(URL, "abc", runner([failed, running]))


def test_another_pull_requests_failed_group_does_not_refuse():
    assert queue_exit.require_fresh(URL, "abc", runner([run_of(8, "failure", run_id=5)])) is None


def test_an_incomplete_listing_refuses_the_queue():
    with pytest.raises(SwarmError, match="could not list every merge group run since head abc landed"):
        queue_exit.require_fresh(URL, "abc", runner([run_of(7, "success")], [], total=5))
