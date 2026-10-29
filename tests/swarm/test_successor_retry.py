import json

import pytest

from scripts.swarm import launch_job
from scripts.swarm.store import MASTER
from scripts.swarm.tick import Placed, SpawnError, tick
from tests.swarm.test_launch_job import DetachedRuntime
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store


class RoutedFailures(FakeRuntime):
    def __init__(self, *failures):
        super().__init__()
        self.failures, self.asked = list(failures), []

    def spawn(self, config, lane, name, task):
        if lane != MASTER:
            self.asked.append((name, task.get("avoid_harness", "")))
            if self.failures:
                raise self.failures.pop(0)
        return super().spawn(config, lane, name, task)


def _successor(store, *failures):
    store.put_handoff("sw", "t1", "pushed, waiting on checks")
    return tasks(("t1", "eng")), RoutedFailures(*failures)


def test_a_successor_retries_a_refused_routed_spawn_on_another_route(store):
    ledger, runtime = _successor(store, SpawnError("route refused", harness="codex"))
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    name = ledger.rows["t1"]["claimed_by"]
    assert runtime.asked == [(name, ""), (name, "codex")]
    assert runtime.spawned == [("eng", name, "t1")]
    assert any(action.startswith(f"retrying {name} for t1 away from codex") for action in actions)
    assert f"spawned {name} for t1" in actions
    assert ledger.rows["t1"]["state"] == "claimed"
    assert store.launch_failure("sw", "t1") == "route refused"
    assert name in runtime.reaped


def test_two_failed_successor_spawns_retry_once_then_reopen_the_task(store):
    ledger, runtime = _successor(
        store, SpawnError("route refused", harness="codex"), SpawnError("second route refused", harness="claude")
    )
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    name = runtime.asked[0][0]
    assert runtime.asked == [(name, ""), (name, "codex")]
    assert any(action.startswith(f"retrying {name} for t1") for action in actions)
    assert any(action.startswith("spawn failed for t1, task t1 reopened") for action in actions)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert store.claimant("sw", "t1") is None
    assert [row["state"] for row in store.launches("sw")] == ["failed"]
    assert store.launch_failure("sw", "t1") == "second route refused"
    assert store.failed_harness("sw", "t1") == "claude"


@pytest.mark.parametrize(
    "handoff,error",
    [
        (False, SpawnError("route refused", harness="codex")),
        (True, SpawnError("init-agent timed out", "ambiguous", harness="codex")),
        (True, SpawnError("route refused")),
    ],
)
def test_only_a_successor_with_a_refused_routed_spawn_retries(store, handoff, error):
    if handoff:
        store.put_handoff("sw", "t1", "pushed, waiting on checks")
    ledger, runtime = tasks(("t1", "eng")), RoutedFailures(error)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert len(runtime.asked) == 1
    assert not any(action.startswith("retrying") for action in actions)
    assert ledger.rows["t1"]["state"] == "open"


class RoutedJobs(DetachedRuntime):
    def __init__(self, home):
        super().__init__(home)
        self.asked, self.at = [], 1000

    def spawn(self, config, lane, name, task):
        placed = super().spawn(config, lane, name, task)
        if lane == MASTER:
            return placed
        self.asked.append((name, task.get("avoid_harness", "")))
        return Placed("", placed.harness, launch_job={**placed.launch_job, "started_at": self.at})


def _refused(tmp_path, name, message, harness):
    error = {"message": message, "status": "refused", "pane_id": "", "harness": harness}
    (tmp_path / f"{name}.json").write_text(json.dumps({"error": error}))


@pytest.fixture
def jobs(store, tmp_path, monkeypatch):
    from scripts.swarm import runtime as runtime_module

    monkeypatch.setattr(launch_job, "spawn", lambda job: None)
    monkeypatch.setattr(runtime_module, "_opened_pane", lambda name, at: "")
    store.put_handoff("sw", "t1", "pushed, waiting on checks")
    ledger, runtime = tasks(("t1", "eng")), RoutedJobs(tmp_path)
    tick("sw", store, ledger, runtime, now_ms=1000)
    store.update("sw", state="paused")
    return ledger, runtime


def test_a_workstation_successor_job_retries_then_reopens_after_two_refused_routes(store, tmp_path, jobs):
    ledger, runtime = jobs
    name = runtime.jobs[0]
    _refused(tmp_path, name, "route refused", "claude")
    runtime.at = 2000
    actions = tick("sw", store, ledger, runtime, now_ms=2000)
    assert runtime.asked == [(name, ""), (name, "claude")]
    assert any(action.startswith(f"retrying {name} for t1 away from claude") for action in actions)
    assert ledger.rows["t1"]["claimed_by"] == name
    assert not (tmp_path / f"{name}.json").exists()
    saved = json.loads(store.redis.hget(store.key("sw", launch_job.KEY), name))
    assert saved["placed"]["launch_job"]["started_at"] == 1000
    _refused(tmp_path, name, "second route refused", "codex")
    actions = tick("sw", store, ledger, runtime, now_ms=3000)
    assert len(runtime.asked) == 2
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert store.launch_failure("sw", "t1") == "second route refused"
    assert [row["state"] for row in store.launches("sw")] == ["failed"]
    assert not store.redis.hgetall(store.key("sw", launch_job.KEY))


def test_a_retried_workstation_successor_reopens_at_the_end_of_its_first_launch_window(store, tmp_path, jobs):
    ledger, runtime = jobs
    name = runtime.jobs[0]
    _refused(tmp_path, name, "route refused", "claude")
    runtime.at = 200_000
    tick("sw", store, ledger, runtime, now_ms=200_000)
    assert ledger.rows["t1"]["claimed_by"] == name
    tick("sw", store, ledger, runtime, now_ms=301_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert store.launch_failure("sw", "t1") == "workstation launch job timed out"
