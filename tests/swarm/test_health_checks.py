import subprocess

from scripts.swarm.health import checks
from scripts.swarm.health.findings import Limits

URL = "https://github.com/o/r/pull/7"
PENDING = "lint\tpass\t8s\thttps://x/1\nunit (3.11, 1)\tpending\t0\thttps://x/2\n"
PASSED = "lint\tpass\t8s\thttps://x/1\nunit (3.11, 1)\tpass\t12s\thttps://x/2\n"


def runner(out, code=0, seen=None):
    def run(argv, **_):
        if seen is not None:
            seen.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    return run


def raising(argv, **_):
    raise subprocess.TimeoutExpired(argv, 20)


def test_a_pull_request_with_a_pending_check_waits_on_checks():
    seen = []
    assert checks.pending(URL, runner(PENDING, 8, seen)) is True
    assert seen == [["gh", "pr", "checks", URL]]


def test_finished_failed_or_unreadable_checks_do_not_wait():
    assert checks.pending(URL, runner(PASSED)) is False
    assert checks.pending(URL, runner("lint\tfail\t8s\thttps://x/1\n", 1)) is False
    assert checks.pending(URL, raising) is False


def test_only_idle_holders_of_a_pull_request_task_are_probed():
    tasks = [
        {"id": "t1", "state": "pr", "pr_url": URL},
        {"id": "t2", "state": "pr", "pr_url": URL},
        {"id": "t3", "state": "claimed", "pr_url": ""},
    ]
    agents = [
        {"name": "sw-eng-1", "lane": "eng", "task": "t1", "idle_ticks": 4},
        {"name": "sw-eng-2", "lane": "eng", "task": "t2", "idle_ticks": 1},
        {"name": "sw-eng-3", "lane": "eng", "task": "t3", "idle_ticks": 9},
    ]
    seen = []
    assert checks.waiting(agents, tasks, Limits(), runner(PENDING, 8, seen)) == {"t1"}
    assert len(seen) == 1
