import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, ledger_events, merge_queue, queue_priority, queue_record
from scripts.swarm.store import SwarmError
from tests.swarm.test_cli import env
from tests.swarm.test_merge_queue import swarm_of
from tests.swarm.test_merge_queue_priority import READ_PULL, URL, FakeQueue

pytestmark = pytest.mark.unit
_fixture = env


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

    def prioritize(url, journal):
        assert isinstance(journal, queue_record.Journal) and journal.url == url
        return reorder(url, run=queue.run, journal=journal)

    monkeypatch.setattr(merge_queue, "prioritize", prioritize)
    original_graphql = merge_queue.graphql

    def graphql(query, variables, run):
        if run is subprocess.run:
            assert query == merge_queue.STATE
            assert variables["url"] in (URL, URL + "0")
            return original_graphql(query, variables, queue.run)
        return original_graphql(query, variables, run)

    monkeypatch.setattr(merge_queue, "graphql", graphql)
    groups = [{"position": 1, "head": "group-head", "pull": URL[:-1] + "1"}]

    def read_groups(journal):
        assert journal.url in (URL, URL + "0")
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


def test_tick_applies_priority_after_blocking_tests_complete(env, monkeypatch):
    from tests.swarm.test_delivery import FakeHerdr

    store, ledger, runtime = env
    assert cli.main(["sw", "create", "--repo", "/repo"]) == 0
    store.redis.hset(
        store.key("sw", queue_priority.PENDING),
        URL,
        json.dumps({"repo": "o/r", "group_head": "old-group", "run_id": 42, "sender": "master"}),
    )
    applied = []

    def rest(path, run):
        assert path == "repos/o/r/actions/runs/42"
        return {"status": "completed"}

    def reorder(url, journal):
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
    actions = cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert applied == [URL]
    assert f"applied deferred merge queue priority for {URL}" in actions
    assert not store.redis.hgetall(store.key("sw", queue_priority.PENDING))


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

    def reorder(url, journal):
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
    assert saved == {"repo": "o/r", "group_head": "group-head", "run_id": 42, "sender": "engineer@a1b2c3-0001"}
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
        lambda url, journal: applied.append(url) or {"url": url, "displaced": [], "errors": []},
    )
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [f"applied deferred merge queue priority for {URL}"]
    assert applied == [URL]


def test_refused_reorder_keeps_pending_request_for_retry(priority, monkeypatch, capsys):
    p = priority
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    capsys.readouterr()
    p.run["status"] = "completed"
    monkeypatch.setattr(merge_queue, "prioritize", lambda url, journal: {"displaced": [], "errors": ["queue refused"]})
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

    def reorder(url, journal):
        assert isinstance(journal, queue_record.Journal) and journal.url == url
        applied.append(url)
        return {"url": url, "displaced": [], "errors": []}

    monkeypatch.setattr(merge_queue, "rest", rest)
    monkeypatch.setattr(merge_queue, "prioritize", reorder)
    assert queue_priority.refresh(p.store, "sw", p.tasks) == [f"applied deferred merge queue priority for {URL}0"]
    assert applied == [URL + "0"]
    assert list(p.store.redis.hgetall(p.store.key("sw", queue_priority.PENDING))) == [URL]


def test_priority_help_explains_operator_lift(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["sw", "merge", "--help"])
    assert stopped.value.code == 0
    assert " ".join(capsys.readouterr().out.split()).endswith(
        "--lift operator: prioritize immediately despite a running head group"
    )
