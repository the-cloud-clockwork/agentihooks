import json
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, merge_queue, queue_priority, queue_record
from scripts.swarm.store import SwarmError
from tests.swarm.test_cli import env
from tests.swarm.test_merge_queue import swarm_of
from tests.swarm.test_merge_queue_priority import URL, FakeQueue

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
_fixture = env


@pytest.fixture
def priority(monkeypatch, tmp_path):
    store = swarm_of("master")
    store.config = lambda slug: None
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    tasks = [{"pr_url": URL[:-1] + "2", "claimed_by": "ci@owner"}]
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(tasks=lambda slug: tasks))
    queue = FakeQueue()
    reorder = merge_queue.prioritize
    monkeypatch.setattr(merge_queue, "prioritize", lambda url, journal: reorder(url, run=queue.run, journal=journal))
    groups = [{"position": 1, "head": "group-head", "pull": URL[:-1] + "1"}]
    monkeypatch.setattr(queue_record.Journal, "groups", lambda self: groups)
    monkeypatch.setattr(queue_record.Journal, "write", lambda self, *args, **kwargs: None)
    observed = {"id": 42, "head_sha": "group-head", "status": "in_progress"}
    reads = []
    original = merge_queue.rest

    def rest(path, run):
        if "/actions/" not in path:
            return original(path, run)
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
    monkeypatch.setattr(cli.clearance, "is_operator", lambda store, who: False)
    assert cli.main(["sw", "merge", "priority", URL, "--lift"]) == 1
    assert "only the operator" in capsys.readouterr().err
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
    assert "head group can be read" in capsys.readouterr().err
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
