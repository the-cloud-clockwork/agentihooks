from scripts.doctor import detect
from scripts.swarm.health.findings import Finding

STALE = Finding("stale claim", "watch-eng-1", "no change for 40 minutes", ("task t1",), "30 minutes", 40)


def test_a_failing_detector_is_named_and_the_others_still_report():
    def broken():
        raise RuntimeError("journal unreadable")

    found, failed = detect.collect({"spawn": broken, "health": lambda: [STALE]})
    assert found == [STALE]
    assert failed == ["the spawn detector failed: RuntimeError: journal unreadable"]


def test_ci_reads_only_the_pull_requests_of_tasks_waiting_in_review():
    tasks = [
        {"state": "pr", "pr_url": "https://github.com/o/r/pull/9"},
        {"state": "pr", "pr_url": "https://github.com/o/r/pull/9"},
        {"state": "done", "pr_url": "https://github.com/o/r/pull/3"},
        {"state": "pr", "pr_url": ""},
        {"state": "pr", "pr_url": "https://github.com/o/other/pull/12"},
    ]
    assert detect.open_pulls(tasks) == [("o/other", 12), ("o/r", 9)]
