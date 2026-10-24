import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, merge_path
from scripts.swarm.store import SwarmError

pytestmark = pytest.mark.unit

SINCE = 1_792_000_000.0
UNTIL = SINCE + 4 * 3600
WINDOW = (SINCE, UNTIL)
GH = {"cwd": "/repo", "capture_output": True, "text": True, "timeout": 60}


def stamp(offset_s):
    return merge_path._stamp(SINCE + offset_s)


def run(run_id, event, conclusion, offset_s=60, spent_s=600, **fields):
    return {
        "id": run_id,
        "event": event,
        "head_branch": "feature",
        "status": "completed",
        "conclusion": conclusion,
        "created_at": stamp(offset_s),
        "run_started_at": stamp(offset_s + 30),
        "updated_at": stamp(offset_s + 30 + spent_s),
        "run_attempt": 1,
        **fields,
    }


def pull(created_s, merged_s=None, events=()):
    return {
        "url": "https://github.com/o/r/pull/1",
        "createdAt": stamp(created_s),
        "mergedAt": None if merged_s is None else stamp(merged_s),
        "timelineItems": {"totalCount": len(events), "nodes": list(events)},
    }


def added(offset_s):
    return {"__typename": "AddedToMergeQueueEvent", "createdAt": stamp(offset_s)}


def removed(offset_s, reason):
    return {"__typename": "RemovedFromMergeQueueEvent", "createdAt": stamp(offset_s), "reason": reason}


RUNS = [
    run(1, "pull_request", "success"),
    run(2, "pull_request", "failure"),
    run(3, "pull_request", "cancelled", run_attempt=2),
    run(4, "pull_request", "success"),
    run(5, "merge_group", "success", spent_s=600),
    run(6, "merge_group", "failure", spent_s=1230),
    run(7, "merge_group", "success", spent_s=300, status="in_progress"),
    run(14, "merge_group", "success", spent_s=915),
    run(8, "push", "success"),
    run(9, "pull_request", "success", offset_s=-60),
    run(10, "pull_request", "success", offset_s=4 * 3600),
    run(11, "push", "success", offset_s=0, head_branch="dev"),
    run(12, "pull_request", "failure", head_branch="dev"),
    run(13, "pull_request", "cancelled", head_branch="promotion/abc"),
]
PULLS = [
    pull(-3600, 1800, (added(600), removed(1800, "MERGED"))),
    pull(0, 3000, (added(900), removed(1500, "FAILED_CHECKS"), added(1600), removed(3000, "MERGED"))),
    pull(-7200, None, (added(-60), removed(120, "MANUAL"))),
    pull(-7200, -60, (removed(-60, "MERGED"),)),
    pull(-7200, None, (removed(3600, "merged"), removed(3700, None))),
]
JOBS = {
    5: [["anton-oss-queue"], ["anton-oss-light"], ["anton-oss-queue", "self-hosted"]],
    6: [["anton-oss-queue"]],
    7: [["anton-oss-queue"]] * 5,
    14: [["anton-oss"], ["anton-oss-batch"]],
}


def test_counts_the_merge_path_inside_the_window_only():
    found = merge_path.count(RUNS, PULLS, JOBS, 3, WINDOW)
    assert found == {
        "hours": 4.0,
        "merges": 2,
        "merges_per_hour": 0.5,
        "tests_runs": {"merge_group": 4, "pull_request": 4, "push": 2},
        "tests_runs_per_merge": {"merge_group": 2.0, "pull_request": 2.0, "push": 1.0},
        "cancelled_pull_request_runs": 1,
        "reruns": 1,
        "enqueues": 3,
        "removals": {"FAILED_CHECKS": 1, "MANUAL": 1, "MERGED": 2, "merged": 1, "unknown": 1},
        "head_failure_rate": 0.33,
        "failed_queue_runs": 1,
        "queue_cycle_minutes": 15.2,
        "standard_jobs_per_queue_run": 1.3,
        "open_ready_pull_requests": 3,
        "lead_time_minutes": 70.0,
    }


@pytest.mark.parametrize(
    ("seconds", "merged_at", "hours", "per_hour"),
    [(100, (50,), 0.03, 36.0), (360_000, (), 100.0, 0.0), (7000, (10, 20), 1.94, 1.03)],
)
def test_window_hours_and_merges_an_hour_round_to_two_places(seconds, merged_at, hours, per_hour):
    pulls = [pull(0, at) for at in merged_at]
    found = merge_path.count([], pulls, {}, 0, (SINCE, SINCE + seconds))
    assert (found["hours"], found["merges_per_hour"]) == (hours, per_hour)


def test_rates_and_medians_without_data_are_unknown_never_zero():
    found = merge_path.count([], [], {}, 0, WINDOW)
    assert found["tests_runs_per_merge"] == {}
    assert found["head_failure_rate"] is None
    assert found["queue_cycle_minutes"] is None
    assert found["standard_jobs_per_queue_run"] is None
    assert found["lead_time_minutes"] is None


def test_the_ledger_line_is_plain_and_within_the_comment_limit():
    line = merge_path.render("o/agentihooks", merge_path.count(RUNS, PULLS, JOBS, 3, WINDOW))
    assert line == (
        "agentihooks, 4.0 hours: 2 merges, 0.5 an hour. Runs 4 queue, 4 head, 2 push. 1 cancelled, 1 reruns. "
        "3 enqueues, removed 1 failed checks, 1 manual, 1 unknown. Heads fail 33 percent, 1 queue runs failed. "
        "Queue cycle 15.2 minutes, 1.3 standard jobs. 3 open ready. Lead 70.0 minutes."
    )
    assert len(line.split()) <= 50


def test_an_empty_window_renders_none_and_unknown():
    found = {**merge_path.count([], [], {}, 0, WINDOW), "head_failure_rate": None}
    assert merge_path.render("host/o/agentihooks-bundle", found) == (
        "agentihooks bundle, 4.0 hours: 0 merges, 0.0 an hour. Runs none. 0 cancelled, 0 reruns. "
        "0 enqueues, removed none. Heads fail unknown percent, 0 queue runs failed. "
        "Queue cycle unknown minutes, unknown standard jobs. 0 open ready. Lead unknown minutes."
    )


def test_the_head_failure_rate_renders_as_whole_percent():
    found = {**merge_path.count([], [], {}, 0, WINDOW), "head_failure_rate": 0.75}
    assert "Heads fail 75 percent," in merge_path.render("o/r", found)


def test_stamps_are_utc_whatever_the_local_zone(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert merge_path._stamp(0) == "1970-01-01T00:00:00Z"
    finally:
        monkeypatch.undo()
        time.tzset()


class Gh:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        returncode, stdout, *stderr = reply
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr[0] if stderr else "")


def page(runs, total):
    return 0, json.dumps({"total_count": total, "workflow_runs": runs})


def runs_path(event, page_number=1):
    branch = "&branch=dev" if event == "push" else ""
    return (
        f"repos/{{owner}}/{{repo}}/actions/workflows/test.yml/runs?event={event}{branch}"
        f"&created={stamp(0)}..{stamp(4 * 3600)}&exclude_pull_requests=true&per_page=100&page={page_number}"
    )


def test_runs_are_read_page_by_page_until_the_total_is_listed():
    gh = Gh([page(RUNS[:1], 2), page(RUNS[1:2], 2)])
    assert merge_path.read_runs("/repo", "merge_group", WINDOW, gh) == RUNS[:2]
    assert gh.calls == [
        (["gh", "api", runs_path("merge_group", 1)], GH),
        (["gh", "api", runs_path("merge_group", 2)], GH),
    ]


def test_an_empty_listing_reads_one_page():
    gh = Gh([page([], 0)])
    assert merge_path.read_runs("/repo", "push", WINDOW, gh) == []
    assert gh.calls == [(["gh", "api", runs_path("push")], GH)]


def test_a_window_past_the_listing_cap_is_refused_not_undercounted():
    full = [run(n, "push", "success") for n in range(100)]
    gh = Gh([page(full, 1001)] * 10)
    with pytest.raises(SwarmError, match="^listed 1000 of 1001 push Tests runs; shorten the window$"):
        merge_path.read_runs("/repo", "push", WINDOW, gh)


@pytest.mark.parametrize(("stderr", "message"), [("", "GitHub API failed"), ("boom\n", "boom")])
def test_a_failed_gh_is_a_swarm_error_with_its_reason(stderr, message):
    with pytest.raises(SwarmError, match=f"^{message}$"):
        merge_path.read_runs("/repo", "push", WINDOW, Gh([(1, "", stderr)]))


def test_a_missing_gh_is_a_swarm_error():
    with pytest.raises(SwarmError, match="^GitHub API: no gh$"):
        merge_path.read_jobs("/repo", 5, Gh([OSError("no gh")]))


def test_jobs_are_read_as_runner_labels():
    gh = Gh([(0, '["anton-oss-queue"]\n["anton-oss-light", "x"]\n')])
    assert merge_path.read_jobs("/repo", 5, gh) == [["anton-oss-queue"], ["anton-oss-light", "x"]]
    path = "repos/{owner}/{repo}/actions/runs/5/jobs?per_page=100"
    assert gh.calls == [(["gh", "api", path, "--paginate", "--jq", ".jobs[] | .labels | @json"], GH)]


def searched(*pages, total=3):
    return 0, "".join(json.dumps({"issueCount": total, "nodes": nodes}) + "\n" for nodes in pages)


def test_updated_pull_requests_past_the_search_cap_are_refused_not_undercounted():
    with pytest.raises(SwarmError, match="^listed 2 of 1200 updated pull requests; shorten the window$"):
        merge_path.read_pulls("/repo", "o/r", SINCE, Gh([searched(PULLS[:2], total=1200)]))


def test_a_pull_request_with_more_queue_events_than_read_is_refused():
    crowded = [{**PULLS[0], "url": f"u{n}", "timelineItems": {"totalCount": 101, "nodes": []}} for n in (1, 2)]
    with pytest.raises(SwarmError, match="^more than 100 merge queue events on u1, u2; shorten the window$"):
        merge_path.read_pulls("/repo", "o/r", SINCE, Gh([searched(crowded, total=2)]))


def test_a_pull_request_with_exactly_the_queue_events_read_is_kept():
    full = {**PULLS[0], "timelineItems": {"totalCount": 100, "nodes": []}}
    assert merge_path.read_pulls("/repo", "o/r", SINCE, Gh([searched([full], total=1)])) == [full]


@pytest.mark.parametrize(
    ("out", "message"),
    [
        ("", "GitHub search answered nothing"),
        ("not json\n", "GitHub search answered unreadably: Expecting value: line 1 column 1 \\(char 0\\)"),
        ('{"issueCount": 1}\nnull\n', "GitHub search answered without a result count"),
        ('{"nodes": []}\n', "GitHub search answered without a result count"),
    ],
)
def test_an_unreadable_or_empty_search_is_a_swarm_error(out, message):
    with pytest.raises(SwarmError, match=f"^{message}$"):
        merge_path.read_pulls("/repo", "o/r", SINCE, Gh([(0, out)]))


def test_collect_reads_runs_jobs_and_searches_with_these_calls():
    gh = Gh(
        [
            (0, "o/r\n"),
            page(RUNS[:4], 4),
            page(RUNS[4:6], 2),
            page(RUNS[8:9], 1),
            (0, '["anton-oss-queue"]\n'),
            (0, ""),
            searched(PULLS[:2], [PULLS[2], {}]),
            (0, json.dumps({"issueCount": 7}) + "\n"),
        ]
    )
    repo, found = merge_path.collect("/repo", WINDOW, gh)
    assert repo == "o/r"
    assert found["merges"] == 2 and found["enqueues"] == 3 and found["open_ready_pull_requests"] == 7
    assert found["standard_jobs_per_queue_run"] == 0.5
    jobs = "repos/{owner}/{repo}/actions/runs/{}/jobs?per_page=100"
    labels = ["--paginate", "--jq", ".jobs[] | .labels | @json"]
    updated = f"q=repo:o/r is:pr base:dev updated:>={stamp(0)}"
    ready = "q=repo:o/r is:pr is:open draft:false base:dev"
    search = ["--jq", ".data.search | @json"]
    assert gh.calls == [
        (["gh", "api", "repos/{owner}/{repo}", "--jq", ".full_name"], GH),
        (["gh", "api", runs_path("pull_request")], GH),
        (["gh", "api", runs_path("merge_group")], GH),
        (["gh", "api", runs_path("push")], GH),
        (["gh", "api", jobs.replace("{}", "5"), *labels], GH),
        (["gh", "api", jobs.replace("{}", "6"), *labels], GH),
        (["gh", "api", "graphql", "--paginate", *search, "-f", f"query={merge_path.PULLS}", "-f", updated], GH),
        (["gh", "api", "graphql", *search, "-f", f"query={merge_path.OPEN_READY}", "-f", ready], GH),
    ]


def command(monkeypatch, found, argv):
    calls = SimpleNamespace(windows=[], posts=[], slugs=[])

    def collect(repo_dir, window):
        calls.windows.append((repo_dir, window))
        return "o/r", found

    def config(slug):
        calls.slugs.append(slug)
        return SimpleNamespace(repo="/repo")

    monkeypatch.setattr(cli, "now_ms", lambda: UNTIL * 1000)
    monkeypatch.setattr(merge_path, "collect", collect)
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(comment_item=lambda *a: calls.posts.append(a)))
    cli.cmd_merge_path(SimpleNamespace(config=config), cli.build_parser().parse_args(["sw", "merge-path", *argv]))
    return calls


def test_the_command_line_prints_and_posts_the_counts(monkeypatch, capsys):
    found = merge_path.count(RUNS, PULLS, JOBS, 3, WINDOW)
    calls = command(monkeypatch, found, ["--hours", "4", "--json", "--post", "phases/p1"])
    assert json.loads(capsys.readouterr().out) == {"repo": "o/r", **found}
    assert calls.slugs == ["sw"] and calls.windows == [("/repo", WINDOW)]
    assert calls.posts == [("sw", "phases/p1", merge_path.render("o/r", found))]


def test_the_command_line_prints_the_line_and_posts_nothing_by_default(monkeypatch, capsys):
    found = merge_path.count(RUNS, PULLS, JOBS, 3, WINDOW)
    calls = command(monkeypatch, found, ["--hours", "0.5"])
    assert capsys.readouterr().out == merge_path.render("o/r", found) + "\n"
    assert calls.windows == [("/repo", (UNTIL - 1800, UNTIL))] and calls.posts == []


@pytest.mark.parametrize("hours", ["0", "-2"])
def test_the_command_line_refuses_an_empty_or_inverted_window(monkeypatch, hours):
    with pytest.raises(SwarmError, match="^merge-path takes hours above zero$"):
        command(monkeypatch, {}, ["--hours", hours])
