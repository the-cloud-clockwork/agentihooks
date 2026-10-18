import json
import subprocess

import pytest

from scripts import ci_queue_rebuild as rebuild
from scripts.swarm import queue_record

pytestmark = pytest.mark.unit
PULL = "https://github.com/org/repo/pull/"
STARTED = "2026-10-16T01:00:00Z"
UNTIL = "2026-10-16T01:17:10Z"


def group(number, position, *, enqueued="2026-10-16T00:30:00Z", jump=False, enqueuer="nestorcolt"):
    return {
        "pull": PULL + str(number),
        "position": position,
        "state": "QUEUED",
        "jump": jump,
        "enqueued_at": enqueued,
        "enqueuer": enqueuer,
        "head": f"h{number}",
        "base": "b",
    }


def added(at, actor="nestorcolt", id=None):
    return {"__typename": "AddedToMergeQueueEvent", "id": id or f"A{at}", "createdAt": at, "actor": {"login": actor}}


def removed(at, reason="manual", actor="nestorcolt", id=None):
    return {
        "__typename": "RemovedFromMergeQueueEvent",
        "id": id or f"R{at}",
        "createdAt": at,
        "actor": {"login": actor},
        "reason": reason,
    }


def cause(groups, events=()):
    return rebuild.cause(PULL + "3505", STARTED, UNTIL, groups, list(events))


def test_an_entry_enqueued_ahead_while_the_group_ran_is_evidence_for_an_unknown_cause():
    groups = [group(1, 1), group(3541, 2, enqueued="2026-10-16T01:16:43Z", jump=True), group(3505, 3)]
    assert cause(groups) == {
        "action": "unknown",
        "gap": rebuild.CORRELATED,
        "correlated": [
            {"pull": PULL + "3541", "enqueuer": "nestorcolt", "enqueued_at": "2026-10-16T01:16:43Z", "jump": True}
        ],
    }


def test_a_plain_enqueue_ahead_is_correlated_without_a_jump():
    found = cause([group(7, 1, enqueued="2026-10-16T01:05:00Z"), group(3505, 2)])
    assert [g["jump"] for g in found["correlated"]] == [False]


def test_an_entry_enqueued_at_the_group_start_second_is_inside_the_window():
    assert cause([group(7, 1, enqueued=STARTED), group(3505, 2)])["correlated"][0]["pull"] == PULL + "7"


def test_an_entry_enqueued_after_the_observation_links_nothing():
    found = cause([group(7, 1, enqueued="2026-10-16T01:20:00Z"), group(3505, 2)])
    assert found == {"action": "unknown", "gap": rebuild.UNSEEN}


def test_a_removal_ahead_is_not_observable_and_stays_unknown_with_the_gap():
    assert cause([group(3505, 1)]) == {"action": "unknown", "gap": rebuild.UNSEEN}
    assert "removal ahead" in rebuild.UNSEEN and "base push" in rebuild.UNSEEN


def test_this_pull_requests_own_removal_names_its_actor_reason_and_event():
    events = [added("2026-10-16T00:30:00Z"), removed("2026-10-16T01:10:00Z", actor="ops", id="RFMQE_1")]
    assert cause([group(1, 1)], events) == {
        "action": "dequeue",
        "actor": "ops",
        "reason": "manual",
        "at": "2026-10-16T01:10:00Z",
        "events": ["RFMQE_1"],
    }


def test_a_removal_then_add_of_this_pull_request_is_a_requeue():
    events = [removed("2026-10-16T01:16:41Z", id="R"), added("2026-10-16T01:16:43Z", actor="ops", id="A")]
    groups = [group(1, 1), group(3505, 2, enqueued="2026-10-16T01:16:43Z")]
    assert cause(groups, events) == {
        "action": "requeue",
        "actor": "ops",
        "removed_reason": "manual",
        "at": "2026-10-16T01:16:43Z",
        "events": ["R", "A"],
    }


def test_duplicate_and_out_of_order_events_link_the_same_requeue():
    removal, add = removed("2026-10-16T01:16:41Z", id="R"), added("2026-10-16T01:16:43Z", actor="ops", id="A")
    groups = [group(1, 1), group(3505, 2, enqueued="2026-10-16T01:16:43Z")]
    assert cause(groups, [add, removal, add, removal]) == cause(groups, [removal, add])


def test_events_outside_the_group_run_link_nothing():
    events = [removed("2026-10-16T00:10:00Z"), added("2026-10-16T00:20:00Z"), removed("2026-10-16T01:30:00Z")]
    assert cause([group(1, 1), group(3505, 2)], events)["action"] == "unknown"


def test_a_pull_request_gone_without_a_removal_event_is_unknown():
    found = cause([group(1, 1)])
    assert found["action"] == "unknown"
    assert "left the queue" in found["gap"]


def node(number, position, head, base, *, jump=False, enqueued="2026-10-16T00:30:00Z"):
    return {
        "position": position,
        "state": "QUEUED",
        "jump": jump,
        "enqueuedAt": enqueued,
        "enqueuer": None if number == 3505 else {"login": "nestorcolt"},
        "headCommit": {"oid": head},
        "baseCommit": {"oid": base},
        "pullRequest": {"number": number, "url": PULL + str(number), "headRefOid": f"pr{number}"},
    }


class Gh:
    def __init__(self, fail=None, more=False):
        self.fail = fail
        self.more = more
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.fail and self.fail in " ".join(command):
            return subprocess.CompletedProcess(command, 1, "", "HTTP 403")
        if command[2] != "graphql":
            data = {
                "repos/org/repo/actions/runs/7": {"created_at": STARTED},
                "repos/org/repo/git/commits/old": {"parents": [{"sha": "oldparent"}]},
            }[command[2]]
            return subprocess.CompletedProcess(command, 0, json.dumps(data), "")
        args = dict(item.split("=", 1) for item in command[4::2])
        query = args.pop("query")
        assert args.pop("url") == PULL + "3505"
        if query == rebuild.QUERY:
            data = {"resource": {"timelineItems": {"nodes": []}}}
        else:
            assert query == queue_record.GROUPS
            nodes = [
                node(3505, 2, "new", "h3541"),
                node(3541, 1, "h3541", "dev", jump=True, enqueued="2026-10-16T01:16:43Z"),
            ]
            info = {"hasNextPage": self.more and not args, "endCursor": "c"}
            data = {
                "viewer": {"login": "tcc-main-ci[bot]"},
                "resource": {"repository": {"mergeQueue": {"entries": {"nodes": nodes, "pageInfo": info}}}},
            }
        return subprocess.CompletedProcess(command, 0, json.dumps({"data": data}), "")


ENV = {
    "GITHUB_REPOSITORY": "org/repo",
    "GITHUB_RUN_ID": "7",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_SHA": "old",
    "GITHUB_REF": "refs/heads/gh-readonly-queue/dev/pr-3505-base",
}


def test_the_record_keeps_old_and_new_group_heads_bases_and_the_linked_cause():
    record = rebuild.record("", ENV, Gh(), now=UNTIL)
    assert record["id"] == "queue-rebuild-7-2"
    assert record["at"] == UNTIL
    assert record["pull"] == PULL + "3505"
    assert record["expected_head"] == "pr3505"
    assert record["old_head"] == "old"
    assert record["old_parent"] == "oldparent"
    assert record["observed_ref_head"] is None
    assert record["new_head"] == "new"
    assert record["new_base"] == "h3541"
    assert record["started"] == STARTED
    assert record["cause"]["gap"] == rebuild.CORRELATED
    assert [g["pull"] for g in record["cause"]["correlated"]] == [PULL + "3541"]
    assert [g["pull"] for g in record["groups"]] == [PULL + "3541", PULL + "3505"]


def test_every_queue_page_is_read_before_linking():
    gh = Gh(more=True)
    record = rebuild.record("", ENV, gh, now=UNTIL)
    pages = [c for c in gh.calls if c[2] == "graphql" and f"query={queue_record.GROUPS}" in c]
    assert len(pages) == 2
    assert len(record["groups"]) == 4


@pytest.mark.parametrize("fail", ["actions/runs", "git/commits", "viewer", "timelineItems"])
def test_an_unreadable_source_still_yields_a_record_naming_the_gap(fail):
    record = rebuild.record("other", ENV, Gh(fail=fail), now=UNTIL)
    assert record["observed_ref_head"] == "other"
    assert record["cause"]["action"] == "unknown"
    assert "HTTP 403" in record["cause"]["gap"]


def test_a_ref_without_a_pull_request_is_recorded_as_a_gap():
    record = rebuild.record("", {**ENV, "GITHUB_REF": "refs/heads/dev"}, Gh(), now=UNTIL)
    assert record["pull"] is None
    assert record["cause"] == {"action": "unknown", "gap": "queue unreadable: the queue ref names no pull request"}


def test_main_prints_the_summary_logs_the_record_and_writes_the_job_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(rebuild.subprocess, "run", Gh())
    assert rebuild.main(["replacement"]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("record queue-rebuild-7-2: unknown cause: the queue entry was enqueued ahead")
    assert out.out.endswith(f"; jump of {PULL}3541 by nestorcolt at 2026-10-16T01:16:43Z\n")
    assert out.out.count("\n") == 1
    logged = json.loads(out.err.split("queue-rebuild ", 1)[1])
    assert logged["observed_ref_head"] == "replacement"
    assert "queue-rebuild-7-2" in summary.read_text()
    assert '"new_head": "new"' in summary.read_text()


@pytest.mark.parametrize(
    "found, expected",
    [
        ({"action": "dequeue", "actor": "ops", "reason": "manual", "at": "t"}, "dequeue by ops (manual)"),
        ({"action": "requeue", "actor": "ops", "removed_reason": "manual", "at": "t"}, "requeue by ops"),
        ({"action": "unknown", "gap": "g"}, "unknown cause: g"),
        ({"action": "unknown", "gap": "line one\nline two"}, "unknown cause: line one line two"),
    ],
)
def test_the_summary_names_each_cause_on_one_line(found, expected):
    assert rebuild.summary({"id": "r", "cause": found}) == f"record r: {expected}"


def test_the_summary_names_a_plain_correlated_enqueue():
    found = {"action": "unknown", "gap": "g", "correlated": [group(7, 1)]}
    assert rebuild.summary({"id": "r", "cause": found}) == (
        f"record r: unknown cause: g; enqueue of {PULL}7 by nestorcolt at 2026-10-16T00:30:00Z"
    )


def test_this_pull_request_itself_is_never_correlated_ahead_of_itself():
    assert cause([group(3505, 1, enqueued="2026-10-16T01:10:00Z")]) == {"action": "unknown", "gap": rebuild.UNSEEN}


def test_an_entry_enqueued_at_the_observation_second_is_inside_the_window():
    assert cause([group(7, 1, enqueued=UNTIL), group(3505, 2)])["correlated"][0]["pull"] == PULL + "7"


@pytest.mark.parametrize("at", [STARTED, UNTIL])
def test_a_removal_at_either_window_edge_is_linked(at):
    assert cause([group(1, 1)], [removed(at)])["at"] == at


def test_the_gap_for_a_pull_request_gone_without_events_is_exact():
    assert cause([group(1, 1)]) == {"action": "unknown", "gap": "the pull request left the queue with no removal event"}


def test_a_removal_without_a_later_add_while_still_queued_is_not_a_requeue():
    assert cause([group(3505, 1)], [removed("2026-10-16T01:10:00Z")]) == {"action": "unknown", "gap": rebuild.UNSEEN}


def test_a_removal_and_add_in_the_same_second_is_a_requeue():
    at = "2026-10-16T01:10:00Z"
    assert cause([group(3505, 1)], [removed(at, id="R"), added(at, id="A")])["action"] == "requeue"


@pytest.mark.parametrize("missing", ["GITHUB_REPOSITORY", "GITHUB_REF"])
def test_a_missing_repository_or_ref_is_recorded_as_a_gap(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    record = rebuild.record("", env, Gh(), now=UNTIL)
    assert record["pull"] is None
    assert record["ref"] == env.get("GITHUB_REF")
    assert record["cause"]["gap"] == "queue unreadable: the queue ref names no pull request"


def test_the_record_keeps_the_queue_ref():
    assert rebuild.record("", ENV, Gh(), now=UNTIL)["ref"] == ENV["GITHUB_REF"]


def test_a_journal_gap_is_the_cause_gap():
    record = rebuild.record("", ENV, Gh(fail="viewer"), now=UNTIL)
    assert record["cause"]["gap"] == "queue unreadable: queue unreadable: HTTP 403"


def test_the_record_time_defaults_to_utc_now_in_github_form(monkeypatch):
    import time
    from datetime import UTC, datetime

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        at = rebuild.record("", ENV, Gh())["at"]
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    stamp = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 60


def _main(monkeypatch, argv, summary=None):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    if summary is None:
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    else:
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(rebuild.subprocess, "run", Gh())
    return rebuild.main(argv)


def _logged(err):
    return json.loads(err.split("queue-rebuild ", 1)[1].splitlines()[0])


def test_main_without_an_observed_head_records_none(monkeypatch, capsys):
    assert _main(monkeypatch, []) == 0
    assert _logged(capsys.readouterr().err)["observed_ref_head"] is None


def test_main_reads_the_observed_head_from_the_command_line(monkeypatch, capsys):
    monkeypatch.setattr(rebuild.sys, "argv", ["ci_queue_rebuild", "replacement"])
    assert _main(monkeypatch, None) == 0
    assert _logged(capsys.readouterr().err)["observed_ref_head"] == "replacement"


def test_the_job_summary_holds_the_record_as_indented_json(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    _main(monkeypatch, ["x"], summary)
    text = summary.read_text()
    assert text.startswith('### Queue rebuild queue-rebuild-7-2\n\n```json\n{\n  "id": ')
    assert text.endswith("\n}\n```\n")


def test_an_unwritable_job_summary_still_prints_the_summary(tmp_path, monkeypatch, capsys):
    assert _main(monkeypatch, ["x"], tmp_path) == 0
    out = capsys.readouterr()
    assert out.out.startswith("record queue-rebuild-7-2: ")
    assert out.out.count("\n") == 1
    assert "\njob summary not written: " in out.err
