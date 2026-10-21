import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, commands, ledger_events, merge_queue, queue_priority, queue_record
from scripts.swarm.store import SwarmError
from tests.swarm.test_cli import env
from tests.swarm.test_merge_queue import swarm_of
from tests.swarm.test_merge_queue_priority import READ_PULL, URL, FakeQueue, Killed

pytestmark = pytest.mark.unit
_fixture = env
GRAPHQL = merge_queue.graphql


@pytest.fixture
def priority(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger_events, "view", READ_PULL)
    store = swarm_of("master")
    store.config = lambda slug: None
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    tasks = [{"pr_url": URL[:-1] + "2", "claimed_by": "ci@owner"}]
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(tasks=lambda slug: tasks))
    queue = FakeQueue()
    reorder = merge_queue.prioritize

    def prioritize(url, journal, **kwargs):
        assert isinstance(journal, queue_record.Journal) and journal.url == url
        return reorder(url, run=queue.run, journal=journal, **kwargs)

    monkeypatch.setattr(merge_queue, "prioritize", prioritize)
    original_graphql = merge_queue.graphql

    def graphql(query, variables, run):
        if run is subprocess.run:
            assert query == merge_queue.STATE
            assert variables["url"] in (URL, URL + "0", URL + "00")
            return original_graphql(query, variables, queue.run)
        return original_graphql(query, variables, run)

    monkeypatch.setattr(merge_queue, "graphql", graphql)
    groups = [{"position": 1, "head": "group-head", "pull": URL[:-1] + "1"}]

    def read_groups(journal):
        assert journal.url in (URL, URL + "0", URL + "00")
        return groups

    monkeypatch.setattr(queue_record.Journal, "groups", read_groups)
    monkeypatch.setattr(queue_record.Journal, "write", lambda self, *args, **kwargs: None)
    observed = {"id": 42, "head_sha": "group-head", "status": "in_progress"}
    reads = []
    original = merge_queue.rest

    def rest(path, run):
        if "/actions/" not in path:
            return original(path, run)
        assert run is subprocess.run
        reads.append(path)
        if "workflows/" in path:
            assert (
                path == "repos/o/r/actions/workflows/test.yml/runs?event=merge_group&head_sha=group-head&per_page=100"
            )
            return {"workflow_runs": [observed.copy()]}
        assert path == "repos/o/r/actions/runs/42"
        return observed.copy()

    monkeypatch.setattr(merge_queue, "rest", rest)
    return SimpleNamespace(store=store, tasks=tasks, queue=queue, groups=groups, run=observed, reads=reads)


def test_deferred_priority_runs_after_original_tests_complete(priority, capsys):
    p = priority
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == 42
    assert queue_priority.refresh(p.store, "sw", p.tasks) == []
    assert p.queue.mutations == []
    p.run["status"] = "completed"
    p.groups[0]["head"] = "next-group"
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [f"applied deferred merge queue priority for {URL}"]
    assert p.reads[-1] == "repos/o/r/actions/runs/42"
    assert p.queue.mutations == [("dequeue", 3), ("dequeue", 2), ("queue", 9), ("queue", 2), ("queue", 3)]
    assert not p.store.redis.hgetall(p.store.key("sw", queue_priority.PENDING))
    assert queue_priority.refresh(p.store, "sw", p.tasks) == []


def test_operator_lift_applies_immediately_and_clears_pending(priority, monkeypatch, capsys):
    p = priority
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    capsys.readouterr()
    monkeypatch.setattr(cli.clearance, "is_operator", lambda store, who: True)
    assert cli.main(["sw", "merge", "priority", URL, "--lift"]) == 0
    assert json.loads(capsys.readouterr().out)["errors"] == []
    assert p.queue.mutations
    assert len(p.reads) == 1
    assert not p.store.redis.hgetall(p.store.key("sw", queue_priority.PENDING))


def test_master_cannot_lift_the_running_group(priority, monkeypatch, capsys):
    def is_operator(store, who):
        assert store is priority.store
        assert who == cli.Who.from_env()
        return False

    monkeypatch.setattr(cli.clearance, "is_operator", is_operator)
    assert cli.main(["sw", "merge", "priority", URL, "--lift"]) == 1
    assert capsys.readouterr().err == "swarm: only the operator lifts a running merge group priority hold\n"
    assert priority.queue.mutations == []
    assert priority.reads == []


@pytest.mark.parametrize("groups", [[], [{"head": None}]])
def test_no_head_group_prioritizes_immediately(priority, groups, capsys):
    priority.groups[:] = groups
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    assert json.loads(capsys.readouterr().out)["errors"] == []
    assert priority.queue.mutations
    assert priority.reads == []


def test_completed_head_group_prioritizes_immediately(priority, capsys):
    priority.run["status"] = "completed"
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    assert json.loads(capsys.readouterr().out)["errors"] == []
    assert priority.queue.mutations


def test_unreadable_queue_never_mutates(priority, monkeypatch, capsys):
    monkeypatch.setattr(queue_record.Journal, "groups", lambda self: None)
    assert cli.main(["sw", "merge", "priority", URL]) == 1
    assert capsys.readouterr().err == "swarm: queue priority waits until the head group can be read\n"
    assert priority.queue.mutations == []


def test_github_outage_retains_deferred_request(priority, monkeypatch, capsys):
    p = priority
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    capsys.readouterr()

    def fail(path, run):
        raise SwarmError("GitHub unavailable")

    monkeypatch.setattr(merge_queue, "rest", fail)
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [
        f"merge queue priority for {URL} waits: GitHub unavailable"
    ]
    assert p.store.redis.hgetall(p.store.key("sw", queue_priority.PENDING))
    assert p.queue.mutations == []


@pytest.mark.parametrize("error", ["", "could not restore https://github.com/o/r/pull/2: refused"])
def test_the_refresh_job_applies_priority_after_blocking_tests_complete(env, monkeypatch, error):
    from scripts.ci_budget import defects, flakes
    from scripts.swarm import ci_speed, grafana_alerts, langfuse, lease, refresh_job

    store, ledger, _ = env
    assert cli.main(["sw", "create", "--repo", "/repo"]) == 0
    assert lease.acquire(store, "sw", commands.hive_id()) is not None
    for module in (ci_speed, defects, flakes, langfuse, grafana_alerts):
        monkeypatch.setattr(module, "refresh", lambda *args: [])
    store.redis.hset(
        store.key("sw", queue_priority.PENDING),
        URL,
        json.dumps({"repo": "o/r", "group_head": "old-group", "run_id": 42, "sender": "master"}),
    )
    applied = []

    def rest(path, run):
        assert path == "repos/o/r/actions/runs/42"
        return {"status": "completed"}

    def reorder(url, journal, **kwargs):
        applied.append(url)
        return {"url": url, "displaced": [], "errors": []}

    def graphql(query, variables, run):
        assert query == merge_queue.STATE
        assert variables == {"url": URL}
        assert run is subprocess.run
        return {"resource": {"state": "OPEN", "baseRefName": "dev"}}

    monkeypatch.setattr(merge_queue, "graphql", graphql)
    monkeypatch.setattr(merge_queue, "rest", rest)
    monkeypatch.setattr(merge_queue, "prioritize", reorder)
    alerts, writes = [], []
    monkeypatch.setattr(ledger, "raise_alert", lambda *args: alerts.append(args), raising=False)
    monkeypatch.setattr(queue_record.Journal, "write", lambda self, *args, **kwargs: writes.append((args, kwargs)))
    if error:
        monkeypatch.setattr(merge_queue, "prioritize", lambda *args, **kwargs: {"displaced": [], "errors": [error]})
    actions = refresh_job.run(store, "sw", ledger, 1_000)
    if error:
        assert alerts == [("sw", f"merge-priority:{URL}", f"Deferred merge priority failed for {URL}: {error}")]
        assert writes == [
            (
                ("priority-failed", URL, "", None, None),
                {"repo": "o/r", "group_head": "old-group", "run_id": 42, "sender": "master", "errors": [error]},
            ),
        ]
        assert store.redis.hgetall(store.key("sw", queue_priority.PENDING))
    else:
        assert applied == [URL]
        assert f"applied deferred merge queue priority for {URL}" in actions
        assert not store.redis.hgetall(store.key("sw", queue_priority.PENDING))
        assert alerts == []


def test_missing_run_resolves_original_group_after_queue_advances(priority, monkeypatch, capsys):
    p = priority
    responses = [[], [{"id": 42, "status": "completed"}]]
    reads = []

    def rest(path, run):
        assert path == "repos/o/r/actions/workflows/test.yml/runs?event=merge_group&head_sha=group-head&per_page=100"
        reads.append(path)
        return {"workflow_runs": responses.pop(0)}

    monkeypatch.setattr(merge_queue, "rest", rest)
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] is None
    p.groups[0]["head"] = "new-group"

    def reorder(url, journal, **kwargs):
        return {"url": url, "displaced": [], "errors": []}

    monkeypatch.setattr(merge_queue, "prioritize", reorder)
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [f"applied deferred merge queue priority for {URL}"]
    assert len(reads) == 2
    assert not responses
    assert not p.store.redis.hgetall(p.store.key("sw", queue_priority.PENDING))


def test_missing_tests_run_waits_for_discovery_then_completion(priority, monkeypatch, capsys):
    p = priority
    responses = [[], [], [{"id": 42, "status": "queued"}]]
    original_rest = merge_queue.rest

    def rest(path, run):
        if "workflows/" in path:
            assert (
                path == "repos/o/r/actions/workflows/test.yml/runs?event=merge_group&head_sha=group-head&per_page=100"
            )
            return {"workflow_runs": responses.pop(0)}
        return original_rest(path, run)

    monkeypatch.setattr(merge_queue, "rest", rest)
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "url": URL,
        "deferred": True,
        "displaced": [],
        "errors": [],
        "repo": "o/r",
        "group_head": "group-head",
        "run_id": None,
    }
    assert queue_priority.refresh(p.store, "sw", p.tasks) == []
    assert queue_priority.refresh(p.store, "sw", p.tasks) == []
    saved = json.loads(p.store.redis.hget(p.store.key("sw", queue_priority.PENDING), URL))
    assert saved == {
        "repo": "o/r",
        "group_head": "group-head",
        "run_id": 42,
        "sender": "engineer@a1b2c3-0001",
        "order": 1,
    }
    assert p.queue.mutations == []
    p.run["status"] = "completed"
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [f"applied deferred merge queue priority for {URL}"]


def test_missing_run_for_a_departed_group_does_not_hold_forever(priority, monkeypatch, capsys):
    p = priority
    monkeypatch.setattr(merge_queue, "rest", lambda path, run: {"workflow_runs": []})
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    capsys.readouterr()
    p.groups.clear()
    applied = []
    monkeypatch.setattr(
        merge_queue,
        "prioritize",
        lambda url, journal, **kwargs: applied.append(url) or {"url": url, "displaced": [], "errors": []},
    )
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [f"applied deferred merge queue priority for {URL}"]
    assert applied == [URL]


def test_refused_reorder_keeps_pending_request_for_retry(priority, monkeypatch, capsys):
    p = priority
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    capsys.readouterr()
    p.run["status"] = "completed"
    monkeypatch.setattr(
        merge_queue, "prioritize", lambda url, journal, **kwargs: {"displaced": [], "errors": ["queue refused"]}
    )
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [f"merge queue priority for {URL} waits: queue refused"]
    assert p.store.redis.hget(p.store.key("sw", queue_priority.PENDING), URL)


@pytest.mark.parametrize(
    "state,base,reason",
    [
        ("MERGED", "dev", "pull request is MERGED"),
        ("CLOSED", "dev", "pull request is CLOSED"),
        ("OPEN", "main", "pull request no longer targets dev"),
    ],
)
@pytest.mark.parametrize("run_status", ["in_progress", "completed", None])
def test_refresh_drops_ineligible_priorities(priority, monkeypatch, state, base, reason, run_status):
    p = priority
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    key = p.store.key("sw", queue_priority.PENDING)
    record = json.loads(p.store.redis.hget(key, URL))
    p.run["status"] = run_status
    if run_status is None:
        record["run_id"] = None
        p.store.redis.hset(key, URL, json.dumps(record))
    p.store.redis.hset(key, URL + "0", json.dumps(record))
    records = []

    def graphql(query, variables, run):
        assert query == merge_queue.STATE
        assert variables in ({"url": URL}, {"url": URL + "0"})
        assert run is subprocess.run
        return {"resource": {"state": state, "baseRefName": base}}

    monkeypatch.setattr(merge_queue, "graphql", graphql)
    monkeypatch.setattr(queue_record.Journal, "write", lambda self, *args, **kwargs: records.append((args, kwargs)))
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [
        f"dropped deferred merge queue priority for {URL}: {reason}",
        f"dropped deferred merge queue priority for {URL}0: {reason}",
    ]
    assert p.store.redis.hgetall(key) == {}
    assert records == [
        (("priority-dropped", URL, "", None, None), {"reason": reason, **record}),
        (("priority-dropped", URL + "0", "", None, None), {"reason": reason, **record}),
    ]
    assert p.queue.mutations == []
    assert queue_priority.refresh(p.store, "sw", p.tasks) == []
    assert len(records) == 2


@pytest.mark.parametrize("unreadable", [False, True])
def test_refresh_keeps_priority_when_pull_request_cannot_be_read(priority, monkeypatch, unreadable):
    p = priority
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    key = p.store.key("sw", queue_priority.PENDING)
    before = p.store.redis.hgetall(key)

    def graphql(query, variables, run):
        assert query == merge_queue.STATE
        assert variables == {"url": URL}
        assert run is subprocess.run
        if unreadable:
            return {"resource": None}
        raise SwarmError("GitHub unavailable")

    monkeypatch.setattr(merge_queue, "graphql", graphql)
    reason = "pull request could not be read" if unreadable else "GitHub unavailable"
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [f"merge queue priority for {URL} waits: {reason}"]
    assert p.store.redis.hgetall(key) == before
    assert p.queue.mutations == []


def test_replay_records_application_and_notifies_the_displaced_owner(priority, monkeypatch, capsys):
    p = priority
    records, sent = [], []
    monkeypatch.setattr(queue_record.Journal, "write", lambda self, *args, **kwargs: records.append((args, kwargs)))
    monkeypatch.setattr(
        queue_priority,
        "InboxStore",
        lambda redis: SimpleNamespace(send=lambda *args, **flags: sent.append((*args, flags))),
    )
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    capsys.readouterr()
    assert records == [
        (
            ("priority-deferred", URL, "", None, None),
            {
                "repo": "o/r",
                "group_head": "group-head",
                "run_id": 42,
                "sender": "engineer@a1b2c3-0001",
                "order": 1,
            },
        )
    ]
    p.run["status"] = "completed"
    queue_priority.refresh(p.store, "sw", p.tasks)
    assert sent == [
        (
            "engineer@a1b2c3-0001",
            "ci@owner",
            f"Your pull request {URL[:-1]}2 was requeued in its original order after priority request {URL}.",
            {"fyi": True},
        )
    ]
    assert records[-1] == (
        ("priority-applied", URL, "", None, None),
        {
            "repo": "o/r",
            "group_head": "group-head",
            "run_id": 42,
            "sender": "engineer@a1b2c3-0001",
            "order": 1,
        },
    )


def test_operator_lift_is_journaled(priority, monkeypatch, capsys):
    records = []
    monkeypatch.setattr(queue_record.Journal, "write", lambda self, *args, **kwargs: records.append((args, kwargs)))
    monkeypatch.setattr(cli.clearance, "is_operator", lambda store, who: True)
    assert cli.main(["sw", "merge", "priority", URL, "--lift"]) == 0
    assert json.loads(capsys.readouterr().out)["errors"] == []
    assert records[0] == (("priority-lift", URL, "", None, None), {})


@pytest.mark.parametrize("missing", [False, True])
def test_one_held_request_does_not_block_a_completed_request(priority, monkeypatch, missing):
    p = priority
    p.store.redis.hset(
        p.store.key("sw", queue_priority.PENDING),
        mapping={
            URL: json.dumps(
                {"repo": "o/r", "group_head": "group-head", "run_id": None if missing else 42, "sender": "master"}
            ),
            URL + "0": json.dumps({"repo": "o/r", "group_head": "other-head", "run_id": 43, "sender": "master"}),
        },
    )
    original_rest = merge_queue.rest

    def rest(path, run):
        assert run is subprocess.run
        if path == "repos/o/r/actions/runs/43":
            return {"status": "completed"}
        if "workflows/" in path:
            assert missing
            return {"workflow_runs": []}
        return original_rest(path, run)

    applied = []

    def reorder(url, journal, **kwargs):
        assert isinstance(journal, queue_record.Journal) and journal.url == url
        applied.append(url)
        return {"url": url, "displaced": [], "errors": []}

    monkeypatch.setattr(merge_queue, "rest", rest)
    monkeypatch.setattr(merge_queue, "prioritize", reorder)
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [f"applied deferred merge queue priority for {URL}0"]
    assert applied == [URL + "0"]
    assert list(p.store.redis.hgetall(p.store.key("sw", queue_priority.PENDING))) == [URL]


@pytest.mark.parametrize("reverse_hash", [False, True])
@pytest.mark.parametrize("retry", [False, True])
def test_deferred_priorities_keep_request_order_and_admit_unqueued_pull(priority, monkeypatch, reverse_hash, retry):
    p = priority
    original_run = p.queue.run

    def run(command, **kwargs):
        if command[2] == "graphql" and "entries(" in command[4]:
            command = ["url=" + URL if item.startswith("url=") else item for item in command]
        return original_run(command, **kwargs)

    monkeypatch.setattr(p.queue, "run", run)
    monkeypatch.setattr(merge_queue, "require_green", lambda *args: None)
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    assert cli.main(["sw", "merge", "priority", URL + "0"]) == 0
    assert p.queue.mutations == []
    p.run["status"] = "completed"
    if reverse_hash:
        original_read = p.store.redis.hgetall
        monkeypatch.setattr(p.store.redis, "hgetall", lambda key: dict(reversed(original_read(key).items())))
    reorder = merge_queue.prioritize
    if retry:

        def refuse_second(url, journal, **kwargs):
            if url == URL + "0":
                return {"displaced": [], "errors": ["admission refused"]}
            return reorder(url, journal, **kwargs)

        monkeypatch.setattr(merge_queue, "prioritize", refuse_second)
    actions = queue_priority.refresh(p.store, "sw", p.tasks)
    if retry:
        assert actions == [
            f"applied deferred merge queue priority for {URL}",
            f"merge queue priority for {URL}0 waits: admission refused",
        ]
        p.run["status"] = "in_progress"
        assert cli.main(["sw", "merge", "priority", URL + "00"]) == 0
        p.run["status"] = "completed"
        monkeypatch.setattr(merge_queue, "prioritize", reorder)
        actions = queue_priority.refresh(p.store, "sw", p.tasks)
        assert actions == [
            f"applied deferred merge queue priority for {URL}0",
            f"applied deferred merge queue priority for {URL}00",
        ]
    else:
        assert actions == [
            f"applied deferred merge queue priority for {URL}",
            f"applied deferred merge queue priority for {URL}0",
        ]
    expected = ["P1", "P9", "P90", "P900", "P2", "P3"] if retry else ["P1", "P9", "P90", "P2", "P3"]
    assert [entry["pullRequest"]["id"] for entry in p.queue.entries] == expected
    assert not p.store.redis.hgetall(p.store.key("sw", queue_priority.PENDING))


@pytest.mark.parametrize("legacy", [False, True])
def test_deferred_priority_keeps_sequence_after_a_process_restart(priority, monkeypatch, legacy):
    p = priority
    record = {"repo": "o/r", "group_head": "group-head", "run_id": 42, "sender": "master"}
    if not legacy:
        p.store.redis.set(p.store.key("sw", "merge-priority-order"), 11)
        record["order"] = 11
    p.store.redis.hset(p.store.key("sw", queue_priority.PENDING), URL + "0", json.dumps(record))
    read = p.store.redis.hgetall
    monkeypatch.setattr(p.store.redis, "hgetall", lambda key: dict(reversed(read(key).items())))
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    p.run["status"] = "completed"
    applied = []

    def reorder(url, journal, **kwargs):
        applied.append(url)
        return {"displaced": [], "errors": []}

    monkeypatch.setattr(merge_queue, "prioritize", reorder)
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [
        f"applied deferred merge queue priority for {URL}0",
        f"applied deferred merge queue priority for {URL}",
    ]
    assert applied == [URL + "0", URL]


def test_deferred_priority_preserves_a_later_records_earlier_success(priority, monkeypatch):
    p = priority
    original_run = p.queue.run

    def run(command, **kwargs):
        if command[2] == "graphql" and "entries(" in command[4]:
            command = ["url=" + URL if item.startswith("url=") else item for item in command]
        return original_run(command, **kwargs)

    monkeypatch.setattr(p.queue, "run", run)
    monkeypatch.setattr(merge_queue, "require_green", lambda *args: None)
    for url in (URL, URL + "0", URL + "00"):
        assert cli.main(["sw", "merge", "priority", url]) == 0
    key = p.store.key("sw", queue_priority.PENDING)
    for url, run_id in ((URL + "0", 43), (URL + "00", 44)):
        record = json.loads(p.store.redis.hget(key, url))
        record["run_id"] = run_id
        p.store.redis.hset(key, url, json.dumps(record))
    states = {42: "in_progress", 43: "completed", 44: "in_progress"}
    monkeypatch.setattr(merge_queue, "rest", lambda path, run: {"status": states[int(path.rsplit("/", 1)[-1])]})
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [
        f"applied deferred merge queue priority for {URL}0",
    ]
    assert [entry["pullRequest"]["id"] for entry in p.queue.entries] == ["P1", "P90", "P2", "P3"]
    states.update({42: "completed", 44: "completed"})
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [
        f"applied deferred merge queue priority for {URL}",
        f"applied deferred merge queue priority for {URL}00",
    ]
    assert [entry["pullRequest"]["id"] for entry in p.queue.entries] == ["P1", "P9", "P90", "P900", "P2", "P3"]
    assert not p.store.redis.hgetall(key)


def test_priority_help_explains_operator_lift(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["sw", "merge", "--help"])
    assert stopped.value.code == 0
    assert " ".join(capsys.readouterr().out.split()).endswith(
        "--lift operator: prioritize immediately despite a running head group"
    )


def draft_target(run, pull="P9"):
    def read(command, **kwargs):
        done = run(command, **kwargs)
        response = json.loads(done.stdout)
        raw = response["data"].get("resource") if command[2] == "graphql" else None
        if raw and raw.get("id") == pull:
            raw["isDraft"] = True
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    return read


def test_refresh_drops_a_deferred_priority_for_a_draft(priority, monkeypatch, capsys):
    p = priority
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    capsys.readouterr()
    monkeypatch.setattr(p.queue, "run", draft_target(p.queue.run))
    p.run["status"] = "completed"
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [
        f"dropped deferred merge queue priority for {URL}: pull request is a draft"
    ]
    assert p.queue.mutations == []
    assert not p.store.redis.hgetall(p.store.key("sw", queue_priority.PENDING))


def test_refresh_requeues_entries_an_interrupted_priority_left_out(priority, monkeypatch):
    from scripts.swarm import staged_compatibility

    p = priority

    def graphql(query, variables, run):
        assert run is subprocess.run
        found = GRAPHQL(query, variables, p.queue.run)
        if found.get("resource") and found["resource"].get("id") == "P2":
            found["resource"]["body"] = "declared"
        return found

    monkeypatch.setattr(merge_queue, "graphql", graphql)
    monkeypatch.setattr(p.queue, "run", draft_target(p.queue.run, "P5"))
    checked, writes, sent = [], [], []
    monkeypatch.setattr(staged_compatibility, "require", lambda *args: checked.append(args))
    monkeypatch.setattr(
        queue_record.Journal, "write", lambda self, *args, **extra: writes.append((self.url, args, extra))
    )

    def inbox(redis):
        assert redis is p.store.redis
        return SimpleNamespace(send=lambda *args, **flags: sent.append((*args, flags)))

    monkeypatch.setattr(queue_priority, "InboxStore", inbox)
    p.tasks += [{"pr_url": URL[:-1] + n, "claimed_by": f"eng@{n}"} for n in ("4", "5")]
    p.queue.entries = [e for e in p.queue.entries if e["pullRequest"]["id"] not in ("P2", "P4", "P5")]
    displaced = queue_priority.Displaced(p.store, "sw")
    for number, head in ((2, "head2"), (3, "head3"), (4, "moved"), (5, "head5")):
        displaced.add({"id": f"P{number}", "url": URL[:-1] + str(number), "headRefOid": head})
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [
        f"left {URL[:-1]}5 out of the merge queue after an interrupted priority: it became a draft",
        f"left {URL[:-1]}4 out of the merge queue after an interrupted priority: its head moved",
        f"requeued {URL[:-1]}2 left out of the merge queue by an interrupted priority",
    ]
    assert p.queue.mutations == [("queue", 2)]
    assert displaced.pulls() == []
    assert checked == [(URL[:-1] + "2", "head2", "declared", subprocess.run)]
    assert writes == [
        (
            URL[:-1] + "5",
            ("priority-requeue-skipped", URL[:-1] + "5", "head5", None, None),
            {"reason": "it became a draft"},
        ),
        (
            URL[:-1] + "4",
            ("priority-requeue-skipped", URL[:-1] + "4", "moved", None, None),
            {"reason": "its head moved"},
        ),
        (URL[:-1] + "2", ("priority-requeue", URL[:-1] + "2", "head2", None, None), {}),
    ]
    assert sent == [
        (
            "swarm",
            f"eng@{n}",
            f"Your pull request {URL[:-1]}{n} left the merge queue in an interrupted priority and stays out "
            f"because {reason}. Queue it again once its checks pass.",
            {"fyi": True},
        )
        for n, reason in (("5", "it became a draft"), ("4", "its head moved"))
    ]


def test_displaced_records_stay_with_their_swarm(priority):
    p = priority
    queue_priority.Displaced(p.store, "sw").add({"id": "P2", "url": URL[:-1] + "2", "headRefOid": "head2"})
    assert queue_priority.Displaced(p.store, "other").pulls() == []
    assert list(p.store.redis.hgetall(p.store.key("sw", queue_priority.DISPLACED))) == [URL[:-1] + "2"]
    assert p.store.redis.get(p.store.key("sw", queue_priority.ORDER)) == "1"


def test_apply_passes_the_entries_already_ahead(priority, monkeypatch):
    p = priority
    seen = []
    monkeypatch.setattr(
        merge_queue,
        "prioritize",
        lambda url, **kwargs: seen.append(kwargs.get("ahead")) or {"displaced": [], "errors": []},
    )
    queue_priority.apply(p.store, "sw", URL, "master", p.tasks, queue_record.Journal(URL), [URL[:-1] + "3"])
    assert seen == [[URL[:-1] + "3"]]


@pytest.mark.parametrize(
    "raw, settled",
    [
        (None, True),
        ({"state": "MERGED", "baseRefName": "dev", "mergeQueueEntry": None}, True),
        ({"state": "OPEN", "baseRefName": "main", "mergeQueueEntry": None}, True),
        ({"state": "OPEN", "baseRefName": "dev", "mergeQueueEntry": {"id": "E2"}}, True),
        ({"state": "OPEN", "baseRefName": "dev", "mergeQueueEntry": None}, False),
    ],
)
def test_settled_entries_need_no_requeue(raw, settled):
    assert queue_priority.settled(raw) is settled


@pytest.mark.parametrize(
    "holder, stale",
    [
        ("a:b:{gone}:x", True),
        ("a:b:{live}:x", False),
        ("c:{gone}:x", False),
        ("a:b:pid:x", False),
        ("x", False),
    ],
)
def test_a_holder_is_stale_only_when_its_process_on_this_hive_is_gone(holder, stale):
    gone = subprocess.Popen(["true"])
    gone.wait()
    assert queue_priority.stale(holder.format(gone=gone.pid, live=os.getpid()), "a:b") is stale


def test_refresh_restores_entries_a_killed_priority_took_out(priority, monkeypatch):
    p = priority
    monkeypatch.setattr(merge_queue, "graphql", lambda query, variables, run: GRAPHQL(query, variables, p.queue.run))
    run = p.queue.run

    def kill(command, **kwargs):
        if "enqueuePullRequest" in " ".join(command) and "id=P9" in command:
            raise Killed
        return run(command, **kwargs)

    monkeypatch.setattr(p.queue, "run", kill)
    with pytest.raises(Killed):
        queue_priority.apply(p.store, "sw", URL, "master", p.tasks, queue_record.Journal(URL))
    assert [e["pullRequest"]["id"] for e in p.queue.entries] == ["P1"]
    monkeypatch.setattr(p.queue, "run", run)
    active = p.store.key("sw", queue_priority.ACTIVE)
    busy = "another merge queue priority is running; try again when it ends"
    hive, live, gone = commands.hive_id(), os.getpid(), subprocess.Popen(["true"])
    gone.wait()
    gone = gone.pid
    checked = []
    monkeypatch.setattr(queue_priority.staged_compatibility, "require", lambda *args: checked.append(args[2]))
    for holder in (f"{hive}:{live}:other", f"elsewhere:{gone}:other"):
        p.store.redis.set(active, holder)
        assert queue_priority.refresh(p.store, "sw", p.tasks) == [f"merge queue priority waits: {busy}"]
        with pytest.raises(SwarmError) as held:
            queue_priority.apply(p.store, "sw", URL, "master", p.tasks, queue_record.Journal(URL))
        assert str(held.value) == busy
        assert p.store.redis.get(active) == holder
    assert [e["pullRequest"]["id"] for e in p.queue.entries] == ["P1"]
    p.store.redis.set(active, f"{hive}:{gone}:killed")
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [
        f"requeued {URL[:-1]}{n} left out of the merge queue by an interrupted priority" for n in (2, 3)
    ]
    assert [e["pullRequest"]["id"] for e in p.queue.entries] == ["P1", "P2", "P3"]
    assert checked == ["", ""]


@pytest.mark.parametrize("head", ["head2", "moved"])
def test_refresh_keeps_an_entry_it_could_not_requeue(priority, monkeypatch, head):
    from scripts.inbox.store import InboxError

    p = priority

    def graphql(query, variables, run):
        if "enqueuePullRequest" in query:
            raise SwarmError("GitHub unavailable")
        return GRAPHQL(query, variables, p.queue.run)

    def refuse(*args, **flags):
        raise InboxError("GitHub unavailable")

    monkeypatch.setattr(merge_queue, "graphql", graphql)
    monkeypatch.setattr(queue_priority, "InboxStore", lambda redis: SimpleNamespace(send=refuse))
    p.queue.entries = [e for e in p.queue.entries if e["pullRequest"]["id"] != "P2"]
    displaced = queue_priority.Displaced(p.store, "sw")
    displaced.add({"id": "P2", "url": URL[:-1] + "2", "headRefOid": head})
    alerts = []
    ledger = SimpleNamespace(raise_alert=lambda *args: alerts.append(args))
    assert queue_priority.refresh(p.store, "sw", p.tasks, ledger) == [
        f"could not requeue {URL[:-1]}2 after an interrupted priority: GitHub unavailable"
    ]
    assert [pull["url"] for pull in displaced.pulls()] == [URL[:-1] + "2"]
    assert alerts == [
        ("sw", f"merge-priority-requeue:{URL[:-1]}2", f"Could not requeue {URL[:-1]}2: GitHub unavailable")
    ]


def test_priority_notifies_the_claimant_of_an_entry_a_conflict_keeps_out(priority, monkeypatch):
    p = priority
    refusal = "gh: Pull request has merge conflicts and Pull request not in mergeable state"
    run = p.queue.run

    def conflicted(command, **kwargs):
        if "enqueuePullRequest" in " ".join(command) and "id=P2" in command:
            return subprocess.CompletedProcess(command, 1, "", refusal)
        done = run(command, **kwargs)
        response = json.loads(done.stdout)
        raw = response["data"].get("resource") if command[2] == "graphql" else None
        if raw and raw.get("id") == "P2":
            raw["mergeable"] = "CONFLICTING"
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    monkeypatch.setattr(p.queue, "run", conflicted)
    monkeypatch.setattr(merge_queue, "graphql", lambda query, variables, run: GRAPHQL(query, variables, p.queue.run))
    writes, sent = [], []
    monkeypatch.setattr(
        queue_record.Journal, "write", lambda self, *args, **extra: writes.append((self.url, args, extra))
    )
    monkeypatch.setattr(
        queue_priority,
        "InboxStore",
        lambda redis: SimpleNamespace(send=lambda *args, **flags: sent.append((*args, flags))),
    )
    with pytest.raises(SwarmError) as failed:
        queue_priority.apply(p.store, "sw", URL, "master", p.tasks, queue_record.Journal(URL))
    assert str(failed.value) == f"could not restore {URL[:-1]}2: {refusal}"
    reason = "it has merge conflicts with dev"
    assert sent == [
        (
            "swarm",
            "ci@owner",
            f"Your pull request {URL[:-1]}2 left the merge queue in an interrupted priority and stays out "
            f"because {reason}. Queue it again once its checks pass.",
            {"fyi": True},
        )
    ]
    assert (
        URL[:-1] + "2",
        ("priority-requeue-skipped", URL[:-1] + "2", "head2", None, None),
        {"reason": reason},
    ) in writes
    assert queue_priority.Displaced(p.store, "sw").pulls() == []
    assert [e["pullRequest"]["id"] for e in p.queue.entries] == ["P1", "P9", "P3"]
    assert queue_priority.refresh(p.store, "sw", p.tasks) == []
    assert p.queue.mutations == [("dequeue", 3), ("dequeue", 2), ("queue", 9), ("queue", 3)]


def test_priority_tells_the_claimant_once_while_it_retries_a_refused_entry(priority, monkeypatch):
    p = priority
    refusal = "gh: Pull request not in mergeable state"
    run, refused = p.queue.run, [True]

    def refusing(command, **kwargs):
        if refused[0] and "enqueuePullRequest" in " ".join(command) and "id=P2" in command:
            return subprocess.CompletedProcess(command, 1, "", refusal)
        done = run(command, **kwargs)
        response = json.loads(done.stdout)
        raw = response["data"].get("resource") if command[2] == "graphql" else None
        if raw and raw.get("id") == "P2":
            raw["mergeable"] = "UNKNOWN"
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    monkeypatch.setattr(p.queue, "run", refusing)
    monkeypatch.setattr(merge_queue, "graphql", lambda query, variables, run: GRAPHQL(query, variables, p.queue.run))
    sent = []
    monkeypatch.setattr(
        queue_priority,
        "InboxStore",
        lambda redis: SimpleNamespace(send=lambda *args, **flags: sent.append((*args, flags))),
    )
    with pytest.raises(SwarmError):
        queue_priority.apply(p.store, "sw", URL, "master", p.tasks, queue_record.Journal(URL))
    notice = (
        "swarm",
        "ci@owner",
        f"Your pull request {URL[:-1]}2 left the merge queue in a priority and could not be queued again: "
        f"{refusal}. The swarm retries it at the end of the queue on every refresh until it is back or stays out "
        "for a reason.",
        {"fyi": True},
    )
    assert sent == [notice]
    assert [(pull["url"], pull["notified"]) for pull in queue_priority.Displaced(p.store, "sw").pulls()] == [
        (URL[:-1] + "2", True)
    ]
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [
        f"could not requeue {URL[:-1]}2 after an interrupted priority: {refusal}"
    ]
    assert sent == [notice]
    refused[0] = False
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [
        f"requeued {URL[:-1]}2 left out of the merge queue by an interrupted priority"
    ]
    assert [e["pullRequest"]["id"] for e in p.queue.entries] == ["P1", "P9", "P3", "P2"]
    assert queue_priority.Displaced(p.store, "sw").pulls() == []


def test_an_unclaimed_refused_entry_waits_to_tell_a_later_claimant(priority, monkeypatch):
    p = priority
    displaced = queue_priority.Displaced(p.store, "sw")
    displaced.add({"id": "P5", "url": URL[:-1] + "5", "headRefOid": "head5"})
    sent = []
    monkeypatch.setattr(
        queue_priority,
        "InboxStore",
        lambda redis: SimpleNamespace(send=lambda *args, **flags: sent.append(args[1])),
    )
    queue_priority.retrying(p.store, p.tasks, displaced, displaced.pulls()[0], "refused")
    assert sent == []
    assert "notified" not in displaced.pulls()[0]
    p.tasks.append({"pr_url": URL[:-1] + "5", "claimed_by": "eng@5"})
    queue_priority.retrying(p.store, p.tasks, displaced, displaced.pulls()[0], "refused")
    assert sent == ["eng@5"]
    assert displaced.pulls()[0]["notified"] is True


def test_a_failed_notice_tells_every_claimant_again(priority, monkeypatch):
    from scripts.inbox.store import InboxError

    p = priority
    displaced = queue_priority.Displaced(p.store, "sw")
    displaced.add({"id": "P5", "url": URL[:-1] + "5", "headRefOid": "head5"})
    p.tasks += [{"pr_url": URL[:-1] + "5", "claimed_by": who} for who in ("eng@5", "eng@6")]
    sent, refused = [], ["eng@6"]

    def send(sender, claimant, text, **flags):
        if claimant in refused:
            raise InboxError("inbox down")
        sent.append(claimant)

    def inbox(redis):
        assert redis is p.store.redis
        return SimpleNamespace(send=send)

    monkeypatch.setattr(queue_priority, "InboxStore", inbox)
    queue_priority.retrying(p.store, p.tasks, displaced, displaced.pulls()[0], "refused")
    assert sent == ["eng@5"]
    assert "notified" not in displaced.pulls()[0]
    refused.clear()
    queue_priority.retrying(p.store, p.tasks, displaced, displaced.pulls()[0], "refused")
    assert sent == ["eng@5", "eng@5", "eng@6"]
    assert displaced.pulls()[0]["notified"] is True


@pytest.mark.parametrize(
    "raw, reason",
    [
        ({"isDraft": True, "headRefOid": "moved", "mergeable": "CONFLICTING"}, "it became a draft"),
        ({"isDraft": False, "headRefOid": "moved", "mergeable": "CONFLICTING"}, "its head moved"),
        ({"isDraft": False, "headRefOid": "head2", "mergeable": "CONFLICTING"}, "it has merge conflicts with dev"),
        ({"isDraft": False, "headRefOid": "head2", "mergeable": "UNKNOWN"}, ""),
    ],
)
def test_blocker_reports_the_first_reason_an_entry_stays_out(raw, reason):
    assert queue_priority.blocker(raw, "head2") == reason


def test_the_state_query_reads_mergeable():
    assert "isDraft mergeable" in merge_queue.FIELDS
