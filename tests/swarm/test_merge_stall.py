import json
import subprocess
from datetime import datetime, timezone

import pytest

AT = 1_800_000_000_000
HOUR = 3_600_000
REPO = "owner/repo"


def iso(at):
    return datetime.fromtimestamp(at / 1000, timezone.utc).isoformat()


@pytest.fixture
def rig(monkeypatch):
    import fakeredis

    from scripts.swarm import status
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("proof", "/repo", max_eng=0, max_ci=0)
    store.create(config)
    state = {"at": AT, "queue": 3, "merged": AT - HOUR, "calls": []}
    monkeypatch.setattr(status, "now_ms", lambda: state["at"])
    monkeypatch.setattr(status.activity, "counts", lambda slug: {})

    def run(command, **kwargs):
        state["calls"].append((command, kwargs))
        assert kwargs["cwd"] == "/repo"
        assert command[:3] == ["gh", "repo", "view"] or command[:2] == ["gh", "api"]
        if command[1] == "api" and command[2] != "graphql":
            assert command[2].startswith("repos/owner/repo/")
        if command[1:3] == ["repo", "view"]:
            assert command == ["gh", "repo", "view", "--json", "nameWithOwner"]
            data = {"nameWithOwner": REPO}
        elif command[2] == "graphql":
            data = {
                "data": {
                    "repository": {
                        "mergeQueue": {"entries": {"totalCount": state["queue"]}}
                        if state["queue"] is not None
                        else None,
                        "pullRequests": {
                            "nodes": [{"mergedAt": iso(state["merged"]), "updatedAt": iso(state["merged"])}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
            if not state.get("history", True):
                data["data"]["repository"]["pullRequests"]["nodes"] = []
        elif "/actions/runs?" in command[2]:
            data = {"workflow_runs": [{"id": 11, "conclusion": "failure"}, {"id": 12, "conclusion": "failure"}]}
        elif "/actions/runs/11/jobs?" in command[2]:
            assert command[2].endswith("&page=1")
            data = {"jobs": [{"name": "Coverage", "conclusion": "failure"}, {"name": "Tests", "conclusion": "success"}]}
        elif "/actions/runs/12/jobs?" in command[2]:
            assert command[2].endswith("&page=1")
            data = {
                "jobs": [{"name": "Coverage", "conclusion": "failure"}, {"name": "Mutation", "conclusion": "failure"}]
            }
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

    monkeypatch.setattr(subprocess, "run", run)
    return store, config, state


def test_health_pass_reports_stalled_merge_queue_and_notifies_master(rig):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, status

    store, config, state = rig
    found = status.findings(store, "proof", config, [], [])
    assert [(f["kind"], f["subject"], f["evidence"]) for f in found] == [
        (
            "merge stall",
            REPO,
            [
                "Queue length: 3",
                "Time since last dev merge: 60 minutes",
                "Most common failed check in the last 10 queue runs: Coverage (2 runs)",
            ],
        )
    ]
    inbox = InboxStore(store.redis)
    sent = ledger_events.findings_pass(inbox, store, "proof", found)
    assert len(sent) == 1
    item = inbox.inbox("master@proof")[0]
    assert item.sender == "swarm"
    assert item.address == "master@proof"
    assert item.ref == f"merge-stall:proof:{AT - HOUR}:{AT}"
    assert item.text == (
        "Merge stall on owner/repo: Dev has had no merge for 60 minutes with 3 queued entries. "
        "Queue length: 3; Time since last dev merge: 60 minutes; "
        "Most common failed check in the last 10 queue runs: Coverage (2 runs)"
    )
    assert found[0]["summary"] == "Dev has had no merge for 60 minutes with 3 queued entries"
    assert found[0]["threshold"] == "sixty minutes without a dev merge while the queue holds entries; observation only"
    from scripts.swarm.health import merge_stall

    assert merge_stall.findings(store, "proof", config.repo, AT)[0].measure == 60


@pytest.mark.parametrize("length,age", [(0, HOUR), (3, HOUR - 1), (0, 2 * HOUR), (3, 0)])
def test_empty_or_recently_merging_queue_has_no_finding(rig, length, age):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, status

    store, config, state = rig
    state.update(queue=length, merged=AT - age)
    assert status.findings(store, "proof", config, [], []) == []
    inbox = InboxStore(store.redis)
    assert ledger_events.findings_pass(inbox, store, "proof", []) == []
    assert inbox.inbox("master@proof") == []


def test_stall_repeats_hourly_even_after_a_verdict_and_resets_after_recovery(rig):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, status

    store, config, state = rig
    inbox = InboxStore(store.redis)

    def pass_health():
        found = status.findings(store, "proof", config, [], [])
        return found, ledger_events.findings_pass(inbox, store, "proof", found)

    found, sent = pass_health()
    assert len(sent) == 1
    status.verdict_store(store, "proof").judge(found[0]["id"], "established", "master", "Observed", AT)
    assert pass_health()[1] == []
    state["at"] += HOUR - 300_000
    assert pass_health()[1] == []
    state["at"] += 300_000
    assert len(pass_health()[1]) == 1
    state["at"] += 300_000
    state["queue"] = 0
    assert pass_health() == ([], [])
    state["at"] += 300_000
    state["queue"] = 3
    assert len(pass_health()[1]) == 1
    assert len(inbox.inbox("master@proof")) == 3


def test_observation_is_cached_and_failed_refresh_keeps_evidence_without_notices(rig, monkeypatch):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, status

    store, config, state = rig
    found = status.findings(store, "proof", config, [], [])
    count = len(state["calls"])
    state["at"] += 299_999
    assert status.findings(store, "proof", config, [], []) == found
    assert len(state["calls"]) == count

    def fail(command, **kwargs):
        raise OSError("GitHub unavailable")

    monkeypatch.setattr(subprocess, "run", fail)
    state["at"] += 1
    assert status.findings(store, "proof", config, [], []) == found
    assert ledger_events.findings_pass(InboxStore(store.redis), store, "proof", found) == []
    assert json.loads(store.redis.get(store.key("proof", "merge-stall")))["error"] == "GitHub unavailable"


def test_last_merge_is_found_past_old_pulls_updated_recently(monkeypatch):
    from scripts.swarm.health import merge_stall

    commands = []
    pages = [
        {
            "nodes": [{"mergedAt": iso(AT - 3 * HOUR), "updatedAt": iso(AT)}],
            "pageInfo": {"hasNextPage": True, "endCursor": "page-two"},
        },
        {
            "nodes": [{"mergedAt": iso(AT - HOUR), "updatedAt": iso(AT - HOUR)}],
            "pageInfo": {"hasNextPage": True, "endCursor": "unneeded"},
        },
    ]

    def run(command, **kwargs):
        commands.append(command)
        pulls = pages.pop(0)
        data = {"data": {"repository": {"mergeQueue": {"entries": {"totalCount": 123}}, "pullRequests": pulls}}}
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert merge_stall.queue("/repo", REPO) == (123, AT - HOUR)
    assert len(commands) == 2
    assert commands[0] == [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={merge_stall.QUERY}",
        "-f",
        "owner=owner",
        "-f",
        "name=repo",
    ]
    assert commands[1] == [*commands[0], "-f", "cursor=page-two"]


def test_failed_check_counts_runs_once_and_reads_all_jobs(monkeypatch):
    from scripts.swarm.health import merge_stall

    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert command[:2] == ["gh", "api"]
        assert len(commands) <= 3
        if "/actions/runs?" in command[2]:
            data = {"workflow_runs": [{"id": 1, "conclusion": "failure"}, {"id": 2, "conclusion": "success"}]}
        elif command[2].endswith("&page=1"):
            data = {"jobs": [{"name": "Coverage", "conclusion": "failure"}] * 100}
        else:
            data = {"jobs": [{"name": "Mutation", "conclusion": "timed_out"}]}
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert merge_stall.failed_checks("/repo", REPO) == "Coverage (1 runs)"
    assert len(commands) == 3
    assert commands[2][2] == "repos/owner/repo/actions/runs/1/jobs?per_page=100&page=2"


@pytest.mark.parametrize(
    "response,error",
    [
        (subprocess.CompletedProcess([], 1, "", "Permission denied"), "Permission denied"),
        (subprocess.CompletedProcess([], 1, "", ""), "GitHub API failed"),
        (subprocess.CompletedProcess([], 0, '{"errors":[{"message":"Not found"}]}', ""), "Not found"),
    ],
)
def test_github_failure_is_visible_without_creating_a_finding(rig, monkeypatch, response, error):
    from scripts.swarm import status

    store, config, state = rig
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: response)
    assert status.findings(store, "proof", config, [], []) == []
    assert json.loads(store.redis.get(store.key("proof", "merge-stall")))["error"] == error


def test_new_merge_resets_the_notice_even_if_it_is_already_an_hour_old(rig):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, status

    store, config, state = rig
    inbox = InboxStore(store.redis)
    found = status.findings(store, "proof", config, [], [])
    assert len(ledger_events.findings_pass(inbox, store, "proof", found)) == 1
    state.update(at=AT + 300_000, merged=AT - HOUR + 300_000)
    found = status.findings(store, "proof", config, [], [])
    assert len(ledger_events.findings_pass(inbox, store, "proof", found)) == 1


def test_no_recent_failed_queue_check_reports_none_observed(monkeypatch):
    from scripts.swarm.health import merge_stall

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, '{"workflow_runs":[]}', "")

    monkeypatch.setattr(subprocess, "run", run)
    assert merge_stall.failed_checks("/repo", REPO) == "none observed"


@pytest.mark.parametrize("conclusion", ["cancelled", None])
def test_failed_jobs_in_cancelled_or_running_queue_runs_are_counted(monkeypatch, conclusion):
    from scripts.swarm.health import merge_stall

    def run(command, **kwargs):
        if "/actions/runs?" in command[2]:
            data = {"workflow_runs": [{"id": 1, "conclusion": conclusion}]}
        else:
            data = {"jobs": [{"name": "Tests", "conclusion": "failure"}]}
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert merge_stall.failed_checks("/repo", REPO) == "Tests (1 runs)"


def test_every_github_request_renews_the_tick_before_waiting(monkeypatch):
    from scripts.swarm import timing
    from scripts.swarm.health import merge_stall

    order = []
    monkeypatch.setattr(timing, "keep", lambda: order.append("renew"))

    def run(command, **kwargs):
        order.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"nameWithOwner":"owner/repo"}', "")

    monkeypatch.setattr(subprocess, "run", run)
    assert merge_stall.request("/repo", ["repo", "view", "--json", "nameWithOwner"]) == {"nameWithOwner": REPO}
    assert order == [
        "renew",
        (
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            {"cwd": "/repo", "capture_output": True, "text": True, "timeout": 20},
        ),
    ]


@pytest.mark.parametrize("length", [0, None])
def test_an_empty_or_absent_dev_queue_returns_no_entries(rig, length):
    from scripts.swarm.health import merge_stall

    store, config, state = rig
    state["queue"] = length
    assert merge_stall.queue(config.repo, REPO) == (0, 0)


@pytest.mark.parametrize("length,age", [(0, HOUR), (None, HOUR), (3, HOUR - 1), (3, 0)])
def test_observation_of_a_queue_without_a_stall_skips_workflow_requests(rig, length, age):
    from scripts.swarm.health import merge_stall

    store, config, state = rig
    state.update(queue=length, merged=AT - age)
    expected_length = 0 if length is None else length
    expected_merged = AT - age if length else 0
    assert merge_stall.observe(config.repo, AT) == {
        "repo": REPO,
        "queue": expected_length,
        "merged": expected_merged,
        "failed": "none observed",
        "streak": 2,
        "head": 11,
        "at": AT,
    }
    runs = [command for command, _ in state["calls"] if "/actions/runs?" in command[-1]]
    assert len(runs) == 1
    assert not any("/jobs?" in command[-1] for command, _ in state["calls"])


def test_most_common_failed_check_wins_over_alphabetical_order(monkeypatch):
    from scripts.swarm.health import merge_stall

    def run(command, **kwargs):
        assert command[:2] == ["gh", "api"]
        if "/actions/runs?" in command[2]:
            data = {
                "workflow_runs": [
                    {"id": 1, "conclusion": "success"},
                    {"id": 2, "conclusion": "skipped"},
                    {"id": 3, "conclusion": "failure"},
                    {"id": 4, "conclusion": "failure"},
                ]
            }
        elif "/runs/3/" in command[2]:
            data = {"jobs": [{"name": "Aardvark", "conclusion": "failure"}, {"name": "Zebra", "conclusion": "failure"}]}
        elif "/runs/4/" in command[2]:
            data = {"jobs": [{"name": "Zebra", "conclusion": "failure"}]}
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert merge_stall.failed_checks("/repo", REPO) == "Zebra (2 runs)"


def test_multiple_github_errors_remain_readable(monkeypatch):
    from scripts.swarm.health import merge_stall
    from scripts.swarm.store import SwarmError

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, '{"errors":[{"message":"First"},{"message":"Second"}]}', "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SwarmError) as error:
        merge_stall.request("/repo", ["repo", "view"])
    assert str(error.value) == "First; Second"


def test_a_merge_stall_does_not_suppress_later_health_notices(rig):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, status

    store, config, state = rig
    found = status.findings(store, "proof", config, [], [])
    found.append({"id": "ceremony/worker", "kind": "ceremony", "subject": "worker", "summary": "Too many writes"})
    inbox = InboxStore(store.redis)
    assert len(ledger_events.findings_pass(inbox, store, "proof", found)) == 2
    assert len(inbox.inbox("master@proof")) == 2


def test_a_queue_without_observed_merge_history_has_no_invented_merge_time_or_finding(rig):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, status
    from scripts.swarm.health import merge_stall

    store, config, state = rig
    state["history"] = False
    assert merge_stall.queue(config.repo, REPO) == (3, 0)
    assert status.findings(store, "proof", config, [], []) == []
    inbox = InboxStore(store.redis)
    assert ledger_events.findings_pass(inbox, store, "proof", []) == []
    assert inbox.inbox("master@proof") == []


@pytest.mark.parametrize("start", [0.0, 100.0])
def test_slow_merge_refresh_budget_uses_elapsed_monotonic_time(rig, monkeypatch, start):
    import time

    from scripts.swarm.health import merge_stall

    store, config, state = rig
    merge_stall.findings(store, "proof", "/repo", AT)
    state["at"] += merge_stall.CACHE_MS
    clock, original = [start], subprocess.run
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def run(command, **kwargs):
        duration = min(14, kwargs["timeout"])
        clock[0] += duration
        if duration < 14:
            raise subprocess.TimeoutExpired(command, duration)
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert len(merge_stall.findings(store, "proof", "/repo", state["at"])) == 1
    assert clock[0] - start < 30


def test_exact_merge_read_deadline_reports_its_exhausted_budget(rig, monkeypatch):
    import time

    from scripts.swarm.health import merge_stall

    store, config, state = rig
    merge_stall.findings(store, "proof", "/repo", AT)
    state["at"] += merge_stall.CACHE_MS
    clock, original, calls = [100.0], subprocess.run, []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def run(command, **kwargs):
        calls.append(command)
        if kwargs["timeout"] < 5:
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        clock[0] += 5
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert len(merge_stall.findings(store, "proof", "/repo", state["at"])) == 1
    cached = json.loads(store.redis.get(store.key("proof", "merge-stall")))
    assert cached["error"] == "GitHub merge observation exhausted its read budget"
    assert len(calls) == 2 and clock[0] == 110.0


def test_positive_remaining_merge_budget_can_finish_a_refresh(rig, monkeypatch):
    import time

    from scripts.swarm.health import merge_stall

    store, config, state = rig
    merge_stall.findings(store, "proof", "/repo", AT)
    cached = json.loads(store.redis.get(store.key("proof", "merge-stall")))
    cached["failed"] = "Previous check"
    store.redis.set(store.key("proof", "merge-stall"), json.dumps(cached))
    state["at"] += merge_stall.CACHE_MS
    clock, original = [100.0], subprocess.run
    durations = iter((8, 1, 0.4, 0.2, 0.2, 0.1))
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def run(command, **kwargs):
        duration = next(durations)
        assert duration <= kwargs["timeout"]
        clock[0] += duration
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    found = merge_stall.findings(store, "proof", "/repo", state["at"])
    assert len(found) == 1
    assert "Coverage (2 runs)" in " ".join(found[0].evidence)


def test_merge_observation_records_github_read_wall_time(rig, capsys):
    from scripts.swarm import timing
    from scripts.swarm.health import merge_stall

    token = timing.SWARM.set("proof")
    try:
        merge_stall.observe("/repo", AT)
    finally:
        timing.SWARM.reset(token)
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    finished = [row for row in events if row.get("phase") == "finished"]
    assert len(finished) == 6
    assert all(row["step"] == "scripts.swarm.health.merge_stall.github_read" and row["wall_s"] >= 0 for row in finished)
