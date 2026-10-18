import json
import subprocess
import time

import pytest

from scripts.swarm import queue_exit
from scripts.swarm.store import SwarmError

URL = "https://github.com/o/r/pull/7"
SINCE = 1_792_350_000_000
PATH = "repos/o/r/actions/runs?event=merge_group&created=>=2026-10-18T19:00:00Z&per_page=100&page={page}"


def run_of(number, conclusion, name="Tests", run_id=1):
    return {
        "id": run_id,
        "name": name,
        "conclusion": conclusion,
        "head_branch": f"gh-readonly-queue/dev/pr-{number}-{'b' * 40}",
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


def runner(*pages, total=None, code=0):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        page = pages[len(calls) - 1]
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
    run = runner([run_of(7, "cancelled"), run_of(7, "success", run_id=2), run_of(8, None, run_id=3)])
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
