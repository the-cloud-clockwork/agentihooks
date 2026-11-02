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
    from scripts.swarm import failed_panes

    ledger, runtime = _successor(store, SpawnError("route refused", pane_id="w:p9", harness="codex"))
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    name = ledger.rows["t1"]["claimed_by"]
    assert failed_panes.held(store, "sw")["w:p9"]["failed_at"] == 1_000
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
        assert (config.slug, lane) == ("sw", "eng")
        self.asked.append((name, task.get("avoid_harness", "")))
        job = {**placed.launch_job, "started_at": self.at, "request": str(self.home / f"{name}.request.json")}
        (self.home / f"{name}.request.json").write_text(json.dumps({"name": name, "job": job}))
        return Placed("", placed.harness, launch_job=job)


def _refused(tmp_path, name, message, harness, pane=""):
    error = {"message": message, "status": "refused", "pane_id": pane, "harness": harness}
    (tmp_path / f"{name}.json").write_text(json.dumps({"error": error}))


def _launching(store, tmp_path, monkeypatch, *ids):
    from scripts.swarm import runtime as runtime_module

    ledger, runtime = tasks(*[(task, "eng") for task in ids]), RoutedJobs(tmp_path)
    runtime.started = []
    monkeypatch.setattr(launch_job, "spawn", runtime.started.append)
    monkeypatch.setattr(runtime_module, "_opened_pane", lambda name, at: "")
    for task in ids:
        store.put_handoff("sw", task, "pushed, waiting on checks")
    tick("sw", store, ledger, runtime, now_ms=1000)
    store.update("sw", state="paused")
    return ledger, runtime


@pytest.fixture
def jobs(store, tmp_path, monkeypatch):
    return _launching(store, tmp_path, monkeypatch, "t1")


def test_every_refused_successor_job_in_one_collection_is_retried(store, tmp_path, monkeypatch):
    ledger, runtime = _launching(store, tmp_path, monkeypatch, "t1", "t2")
    for name in runtime.jobs:
        _refused(tmp_path, name, "route refused", "claude")
    tick("sw", store, ledger, runtime, now_ms=2000)
    assert sorted(runtime.asked) == sorted(
        [(name, "") for name in runtime.jobs[:2]] + [(name, "claude") for name in runtime.jobs[:2]]
    )
    assert [ledger.rows[task]["state"] for task in ("t1", "t2")] == ["claimed", "claimed"]


def test_a_workstation_successor_job_retries_then_reopens_after_two_refused_routes(store, tmp_path, jobs):
    ledger, runtime = jobs
    name = runtime.jobs[0]
    from scripts.swarm import failed_panes

    _refused(tmp_path, name, "route refused", "claude", "w:p8")
    runtime.at = 2000
    actions = tick("sw", store, ledger, runtime, now_ms=2000)
    assert runtime.asked == [(name, ""), (name, "claude")]
    assert runtime.collected == []
    assert failed_panes.held(store, "sw")["w:p8"]["failed_at"] == 2000
    assert any(action.startswith(f"retrying {name} for t1 away from claude") for action in actions)
    assert ledger.rows["t1"]["claimed_by"] == name
    assert not (tmp_path / f"{name}.json").exists()
    saved = json.loads(store.redis.hget(store.key("sw", launch_job.KEY), name))
    assert saved["placed"]["launch_job"]["started_at"] == 1000
    request = json.loads((tmp_path / f"{name}.request.json").read_text())
    assert request == {"name": name, "job": saved["placed"]["launch_job"]}
    assert runtime.started[-1] == saved["placed"]["launch_job"]
    assert (saved["primed"]["avoid_harness"], saved["primed"]["successor_retried"]) == ("claude", True)
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


def test_a_workstation_successor_failing_after_its_pane_opened_reopens_without_a_retry(
    store, tmp_path, jobs, monkeypatch
):
    from scripts.swarm_v2.runtime import codex

    ledger, runtime = jobs
    name = runtime.jobs[0]
    (tmp_path / f"{name}.json").write_text(json.dumps({"placed": {"pane_id": "w:p1", "harness": "claude"}}))

    def complete(config, name, placed, reap):
        raise RuntimeError("canary failed")

    monkeypatch.setattr(codex, "complete", complete)
    actions = tick("sw", store, ledger, runtime, now_ms=2000)
    assert len(runtime.asked) == 1
    assert not any(action.startswith("retrying") for action in actions)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")


def test_steered_moves_both_avoid_fields_to_the_failed_harness_and_marks_the_retry():
    from scripts.swarm.successor_retry import RETRIED, steered

    primed = {"id": "t1", "transfer": {"avoid_harness": "codex", "reason": "exit"}}
    assert steered(primed, "claude") == {
        "id": "t1",
        "avoid_harness": "claude",
        RETRIED: True,
        "transfer": {"avoid_harness": "claude", "reason": "exit"},
    }
    assert primed["transfer"]["avoid_harness"] == "codex"
    assert steered({"id": "t1", "transfer": {}}, "codex") == {
        "id": "t1",
        "transfer": {},
        "avoid_harness": "codex",
        RETRIED: True,
    }


@pytest.mark.parametrize(
    "task,primed,error,harness",
    [
        ({"handoff": "h"}, {}, SpawnError("refused", harness="codex"), "codex"),
        ({}, {}, SpawnError("refused", harness="codex"), ""),
        ({"handoff": "h"}, {"successor_retried": True}, SpawnError("refused", harness="codex"), ""),
        ({"handoff": "h"}, {}, RuntimeError("refused"), ""),
        ({"handoff": "h"}, {}, None, ""),
        ({"handoff": "h"}, {}, SpawnError("down", "unavailable", harness="codex"), ""),
    ],
)
def test_due_names_the_harness_only_for_a_first_refused_successor_spawn(task, primed, error, harness):
    from scripts.swarm.successor_retry import due

    assert due(task, primed, error) == harness


def test_note_reaps_holds_the_failed_pane_and_records_the_failure(store):
    from scripts.swarm import failed_panes
    from scripts.swarm.store import AgentRecord
    from scripts.swarm.successor_retry import note

    record, runtime = AgentRecord("engineer@x-1", "eng", "t1"), FakeRuntime()
    said = note("sw", store, runtime, record, SpawnError("route refused", pane_id="w:p4", harness="codex"), 9)
    assert runtime.reaped == ["engineer@x-1"]
    assert said == "retrying engineer@x-1 for t1 away from codex after: route refused"
    held = {"w:p4": {"agent": "engineer@x-1", "task": "t1", "failed_at": 9, "reason": ""}}
    assert failed_panes.held(store, "sw") == held
    assert (store.launch_failure("sw", "t1"), store.failed_harness("sw", "t1")) == ("route refused", "codex")


def _relaunch_run(store, tmp_path, spawn):
    from types import SimpleNamespace

    from scripts.swarm.store import AgentRecord, SwarmConfig

    result = tmp_path / "engineer.json"
    result.write_text("{}")
    saved = {
        "task": {"id": "t1", "handoff": "h"},
        "primed": {"id": "t1", "handoff": "h"},
        "placed": {"launch_job": {"path": str(result), "started_at": 1000}},
    }
    runtime = SimpleNamespace(spawn=spawn, reap_name=lambda name: True)
    run = SimpleNamespace(slug="sw", store=store, runtime=runtime, config=SwarmConfig("sw", "/repo", 1, 1), now_ms=5)
    return run, AgentRecord("engineer", "eng", "t1"), saved, result


@pytest.mark.parametrize(
    "spawned,message,status,harness",
    [
        (Placed("w:p1", "codex"), "a retried successor launch ran outside a workstation job", "refused", "codex"),
        (RuntimeError("boom"), "boom", "refused", ""),
        (SpawnError("down", "unavailable", harness="codex"), "down", "unavailable", "codex"),
    ],
)
def test_a_failed_relaunch_returns_its_own_error_for_the_reopen(store, tmp_path, spawned, message, status, harness):
    from scripts.swarm.successor_retry import relaunch

    def spawn(config, lane, name, task):
        if isinstance(spawned, Exception):
            raise spawned
        return spawned

    run, agent, saved, result = _relaunch_run(store, tmp_path, spawn)
    said, error = relaunch(run, agent, saved, SpawnError("route refused", harness="claude"))
    assert said == ""
    assert (str(error), error.status, error.harness) == (message, status, harness)
    assert not result.exists()
    assert not store.redis.hgetall(store.key("sw", launch_job.KEY))


def test_relaunch_leaves_a_job_without_a_saved_launch_to_the_reopen(store, tmp_path):
    from scripts.swarm.successor_retry import relaunch

    run, agent, saved, result = _relaunch_run(store, tmp_path, None)
    first = SpawnError("route refused", harness="claude")
    assert relaunch(run, agent, {**saved, "primed": {}}, first) == ("", first)
    assert result.exists()
