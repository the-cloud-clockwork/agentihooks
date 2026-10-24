import json
import subprocess

import pytest

from scripts.swarm import cli, merge_path
from scripts.swarm.store import SwarmError

pytestmark = pytest.mark.unit

SINCE = 1_792_000_000.0
UNTIL = SINCE + 4 * 3600
WINDOW = (SINCE, UNTIL)


def stamp(offset_s):
    return merge_path._stamp(SINCE + offset_s)


def run(run_id, event, conclusion, offset_s=60, spent_s=600, attempt=1, status="completed"):
    return {
        "id": run_id,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": stamp(offset_s),
        "run_started_at": stamp(offset_s + 30),
        "updated_at": stamp(offset_s + 30 + spent_s),
        "run_attempt": attempt,
    }


def pull(created_s, merged_s=None, events=()):
    return {
        "url": "https://github.com/o/r/pull/1",
        "createdAt": stamp(created_s),
        "mergedAt": None if merged_s is None else stamp(merged_s),
        "timelineItems": {"nodes": list(events)},
    }


def added(offset_s):
    return {"__typename": "AddedToMergeQueueEvent", "createdAt": stamp(offset_s)}


def removed(offset_s, reason):
    return {"__typename": "RemovedFromMergeQueueEvent", "createdAt": stamp(offset_s), "reason": reason}


RUNS = [
    run(1, "pull_request", "success"),
    run(2, "pull_request", "failure"),
    run(3, "pull_request", "cancelled", attempt=2),
    run(4, "pull_request", "success"),
    run(5, "merge_group", "success", spent_s=600),
    run(6, "merge_group", "failure", spent_s=1200),
    run(7, "merge_group", "success", spent_s=900, status="in_progress"),
    run(8, "push", "success"),
    run(9, "pull_request", "success", offset_s=-60),
    run(10, "pull_request", "success", offset_s=4 * 3600),
]
PULLS = [
    pull(-3600, 1800, (added(600), removed(1800, "MERGED"))),
    pull(0, 3000, (added(900), removed(1500, "FAILED_CHECKS"), added(1600), removed(3000, "MERGED"))),
    pull(-7200, None, (added(-60), removed(120, "MANUAL"))),
    pull(-7200, -60, (removed(-60, "MERGED"),)),
]
JOBS = {5: [["anton-oss-queue"], ["anton-oss-light"], ["anton-oss-queue", "self-hosted"]], 6: [["anton-oss-queue"]]}


def test_counts_the_merge_path_inside_the_window_only():
    found = merge_path.count(RUNS, PULLS, JOBS, 3, WINDOW)
    assert found == {
        "hours": 4.0,
        "merges": 2,
        "merges_per_hour": 0.5,
        "tests_runs": {"merge_group": 3, "pull_request": 4, "push": 1},
        "tests_runs_per_merge": {"merge_group": 1.5, "pull_request": 2.0, "push": 0.5},
        "cancelled_pull_request_runs": 1,
        "reruns": 1,
        "enqueues": 3,
        "removals": {"FAILED_CHECKS": 1, "MANUAL": 1, "MERGED": 2},
        "head_failure_rate": 0.33,
        "failed_queue_runs": 1,
        "queue_cycle_minutes": 15.0,
        "standard_jobs_per_queue_run": 1.5,
        "open_ready_pull_requests": 3,
        "lead_time_minutes": 70.0,
    }


def test_rates_and_medians_without_data_are_unknown_never_zero():
    found = merge_path.count([], [], {}, 0, WINDOW)
    assert found["tests_runs_per_merge"] == {}
    assert found["head_failure_rate"] is None
    assert found["queue_cycle_minutes"] is None
    assert found["standard_jobs_per_queue_run"] is None
    assert found["lead_time_minutes"] is None
    assert "unknown percent" in merge_path.render("o/r", found)


def test_the_ledger_line_is_plain_and_within_the_comment_limit():
    line = merge_path.render("o/agentihooks", merge_path.count(RUNS, PULLS, JOBS, 3, WINDOW))
    assert line.startswith("agentihooks, 4.0 hours: 2 merges, 0.5 an hour. Runs 3 queue, 4 head, 1 push.")
    assert "removed 1 failed checks, 1 manual." in line
    assert "merged" not in line.lower().replace("merges", "")
    assert "Heads fail 33 percent, 1 queue runs failed. Queue cycle 15.0 minutes, 1.5 standard jobs." in line
    assert len(line.split()) <= 50
    assert "_" not in line and "-" not in line and "/" not in line


class Gh:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        returncode, stdout = reply
        return subprocess.CompletedProcess(argv, returncode, stdout, "boom" if returncode else "")


def page(runs, total):
    return 0, json.dumps({"total_count": total, "workflow_runs": runs})


def test_runs_are_read_page_by_page_until_the_total_is_listed():
    gh = Gh([page(RUNS[:1], 2), page(RUNS[1:2], 2)])
    assert merge_path.read_runs("/repo", "merge_group", WINDOW, gh) == RUNS[:2]
    paths = [argv[2] for argv, _ in gh.calls]
    assert paths[0].startswith("repos/{owner}/{repo}/actions/workflows/test.yml/runs?event=merge_group&created=")
    assert f"created={stamp(0)}..{stamp(4 * 3600)}&" in paths[0]
    assert paths[0].endswith("&page=1") and paths[1].endswith("&page=2")
    assert all(kwargs["cwd"] == "/repo" for _, kwargs in gh.calls)


def test_a_window_past_the_listing_cap_is_refused_not_undercounted():
    full = [run(n, "push", "success") for n in range(100)]
    gh = Gh([page(full, 1001)] * 10)
    with pytest.raises(SwarmError, match="listed 1000 of 1001 push Tests runs"):
        merge_path.read_runs("/repo", "push", WINDOW, gh)


def test_a_failed_or_missing_gh_is_a_swarm_error():
    with pytest.raises(SwarmError, match="boom"):
        merge_path.read_runs("/repo", "push", WINDOW, Gh([(1, "")]))
    with pytest.raises(SwarmError, match="GitHub API"):
        merge_path.read_jobs("/repo", 5, Gh([OSError("no gh")]))


def test_jobs_are_read_as_runner_labels():
    gh = Gh([(0, '["anton-oss-queue"]\n["anton-oss-light", "x"]\n')])
    assert merge_path.read_jobs("/repo", 5, gh) == [["anton-oss-queue"], ["anton-oss-light", "x"]]
    assert "--paginate" in gh.calls[0][0]


def test_collect_joins_every_paginated_search_page():
    search = {"data": {"search": {"nodes": PULLS[:2]}}}
    second = {"data": {"search": {"nodes": [PULLS[2], {}]}}}
    gh = Gh(
        [
            (0, "o/r\n"),
            page(RUNS[:4], 4),
            page(RUNS[4:7], 3),
            page(RUNS[7:8], 1),
            (0, '["anton-oss-queue"]\n'),
            (0, ""),
            (0, ""),
            (0, json.dumps(search) + "\n" + json.dumps(second)),
            (0, json.dumps({"data": {"search": {"issueCount": 7}}})),
        ]
    )
    repo, found = merge_path.collect("/repo", WINDOW, gh)
    assert repo == "o/r"
    assert found["merges"] == 2 and found["enqueues"] == 3 and found["open_ready_pull_requests"] == 7
    assert found["standard_jobs_per_queue_run"] == 0.3
    searches = [argv for argv, _ in gh.calls if argv[1] == "graphql"]
    assert "--paginate" in searches[0] and f"q=repo:o/r is:pr updated:>={stamp(0)}" in searches[0]
    assert "q=repo:o/r is:pr is:open draft:false" in searches[1]


def test_the_command_line_prints_and_posts_the_counts(monkeypatch, capsys):
    posted = []
    store = type("Store", (), {"config": lambda self, slug: type("C", (), {"repo": "/repo"})()})()
    found = merge_path.count(RUNS, PULLS, JOBS, 3, WINDOW)
    monkeypatch.setattr(cli, "now_ms", lambda: UNTIL * 1000)
    monkeypatch.setattr(merge_path, "collect", lambda repo_dir, window: (posted.append(window) or "o/r", found))
    monkeypatch.setattr(
        cli, "LedgerClient", lambda: type("L", (), {"comment_item": lambda self, *a: posted.append(a)})()
    )
    args = cli.build_parser().parse_args(["sw", "merge-path", "--hours", "4", "--json", "--post", "phases/p1"])
    cli.cmd_merge_path(store, args)
    assert json.loads(capsys.readouterr().out) == {"repo": "o/r", **found}
    assert posted == [WINDOW, ("sw", "phases/p1", merge_path.render("o/r", found))]
