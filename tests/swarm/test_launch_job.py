import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.swarm import launch_job
from scripts.swarm.runtime import SPAWN_TIMEOUT_S, HerdrRuntime
from scripts.swarm.store import SwarmConfig
from scripts.swarm.tick import Placed, SpawnError, tick
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store


def test_runtime_prepares_a_detached_job_for_a_workstation_claim(tmp_path, monkeypatch):
    runtime = HerdrRuntime(home=tmp_path)
    runtime._launch = Mock()
    popen = Mock()
    monkeypatch.setattr(launch_job.subprocess, "Popen", popen)
    config = SwarmConfig("sw", "/repo", 1, 1)
    placed = runtime._account_launch(
        config,
        "eng",
        {"id": "t1", "detached_launch": True},
        "engineer",
        ["agentihooks", "--agent", "claude", "--", "--route", "account"],
    )
    assert placed.launch_job["timeout"] == 300
    request = json.loads(Path(placed.launch_job["request"]).read_text())
    assert request["launch"]["task_id"] == "t1"
    assert request["name"] == "engineer" and request["lane"] == "eng"
    runtime._launch.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("late,error", [(False, False), (True, False), (False, True)])
def test_worker_writes_success_failure_and_late_success_results(tmp_path, monkeypatch, late, error):
    monkeypatch.setattr(launch_job.time, "time", lambda: 1)
    config = SwarmConfig("sw", "/repo", 1, 1)
    job = launch_job.start(
        tmp_path,
        config,
        "eng",
        "engineer",
        {"task_id": "t1", "argv": [], "predecessor": None, "controller_epoch": None},
    )
    reaped = []
    monkeypatch.setattr(HerdrRuntime, "reap_name", lambda self, name: reaped.append(name))

    def launch(self, config, lane, **kwargs):
        assert config.slug == "sw" and lane == "eng" and kwargs["name"] == "engineer"
        if error:
            raise SpawnError("launcher failed", "ambiguous", "w:p1", harness="claude")
        if late:
            monkeypatch.setattr(launch_job.time, "time", lambda: 302)
        return Placed("w:p1", "claude", profile_decision={"validation": {"state": "validated"}})

    monkeypatch.setattr(HerdrRuntime, "_launch", launch)
    launch_job.run(Path(job["request"]))
    if late or error:
        with pytest.raises(SpawnError) as raised:
            launch_job.read(job, 302000)
        assert raised.value.status == "ambiguous" and raised.value.pane_id == "w:p1"
        assert raised.value.harness == "claude"
        assert reaped == (["engineer"] if late else [])
    else:
        result = launch_job.read(job, 2000)
        assert result.pane_id == "w:p1"
        assert result.profile_decision == {"validation": {"state": "validated"}}


def test_worker_limits_the_launcher_to_the_remaining_job_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_job.time, "time", lambda: 1)
    job = launch_job.start(tmp_path, SwarmConfig("sw", "/repo", 1, 1), "eng", "engineer", {"task_id": "t1", "argv": []})
    monkeypatch.setattr(launch_job.time, "time", lambda: 2)
    calls = []
    monkeypatch.setattr(launch_job.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))

    def launch(self, *args, **kwargs):
        self.run(["launcher"], timeout=300, text=True)
        return Placed("w:p1", "claude")

    monkeypatch.setattr(HerdrRuntime, "_launch", launch)
    launch_job.run(Path(job["request"]))
    assert calls == [(["launcher"], {"timeout": 299, "text": True})]
    assert launch_job.read(job, 2000).pane_id == "w:p1"


def test_worker_refuses_to_start_after_its_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_job.time, "time", lambda: 1)
    job = launch_job.start(tmp_path, SwarmConfig("sw", "/repo", 1, 1), "eng", "engineer", {"task_id": "t1", "argv": []})
    monkeypatch.setattr(launch_job.time, "time", lambda: 301)
    launch = Mock()
    monkeypatch.setattr(HerdrRuntime, "_launch", launch)
    launch_job.run(Path(job["request"]))
    launch.assert_not_called()
    with pytest.raises(SpawnError, match="timed out"):
        launch_job.read(job, 301000)


class DetachedRuntime(FakeRuntime):
    def __init__(self, home):
        super().__init__()
        self.home = home
        self.jobs = []

    def spawn(self, config, lane, name, task):
        if lane == "master":
            return super().spawn(config, lane, name, task)
        assert task["detached_launch"]
        self.jobs.append(name)
        return Placed(
            "",
            "claude",
            model_source="picker",
            choice="share",
            launch_job={
                "path": str(self.home / f"{name}.json"),
                "started_at": 1000,
                "timeout": 300,
            },
        )


def test_job_is_recoverable_before_dispatch_and_dispatch_failure_reopens_claim(store, tmp_path, monkeypatch):
    ledger, runtime = tasks(("t1", "eng")), DetachedRuntime(tmp_path)

    def dispatch(job):
        name = ledger.rows["t1"]["claimed_by"]
        assert store.claimant("sw", "t1") == name
        assert name in store.redis.hgetall(store.key("sw", launch_job.KEY))
        assert next(row for row in store.launches("sw") if row["agent"] == name)["state"] == "launching"
        raise OSError("cannot start worker")

    monkeypatch.setattr(launch_job, "spawn", dispatch)
    tick("sw", store, ledger, runtime, now_ms=1000)
    assert ledger.rows["t1"]["state"] == "open"
    assert not store.redis.hgetall(store.key("sw", launch_job.KEY))
    assert store.launch_failures("sw")["t1"]


def test_collection_preserves_an_agent_that_finished_before_its_result(store, tmp_path, monkeypatch):
    monkeypatch.setattr(launch_job, "spawn", lambda job: None)
    ledger, runtime = tasks(("t1", "eng")), DetachedRuntime(tmp_path)
    tick("sw", store, ledger, runtime, now_ms=1000)
    name = runtime.jobs[0]
    agent = next(agent for agent in store.agents("sw") if agent.name == name)
    store.put_agent("sw", replace(agent, state="finished"))
    store.update("sw", state="paused")
    (tmp_path / f"{name}.json").write_text(json.dumps({"placed": asdict(Placed("w:p1", "claude"))}))
    tick("sw", store, ledger, runtime, now_ms=2000)
    assert next(agent for agent in store.agents("sw") if agent.name == name).state == "finished"
    assert store.claims("sw", "t1") == 1
    assert not store.redis.hgetall(store.key("sw", launch_job.KEY))


def test_pending_launch_keeps_claim_and_is_not_launched_twice(store, tmp_path, monkeypatch):
    monkeypatch.setattr(launch_job, "spawn", lambda job: None)
    ledger, runtime = tasks(("t1", "eng")), DetachedRuntime(tmp_path)
    tick("sw", store, ledger, runtime, now_ms=1000)
    name = ledger.rows["t1"]["claimed_by"]
    assert runtime.jobs == [name]
    assert store.claimant("sw", "t1") == name
    assert store.claims("sw", "t1") == 0
    assert store.launches("sw")[0]["state"] == "launching"
    tick("sw", store, ledger, runtime, now_ms=2000)
    assert runtime.jobs == [name]
    assert store.claimant("sw", "t1") == name


def test_completed_job_records_the_existing_placed_fields(store, tmp_path, monkeypatch):
    monkeypatch.setattr(launch_job, "spawn", lambda job: None)
    ledger, runtime = tasks(("t1", "eng")), DetachedRuntime(tmp_path)
    tick("sw", store, ledger, runtime, now_ms=1000)
    name = runtime.jobs[0]
    result = Placed(
        "w:p1",
        "claude",
        account="acct",
        model="opus",
        effort="high",
        profile="engineer",
        profile_decision={"validation": {"state": "validated"}},
        launched_at=1100,
        launch_timings={"launched_at": 1100, "returned_at": 1900},
    )
    (tmp_path / f"{name}.json").write_text(json.dumps({"placed": asdict(result)}))
    runtime.live.add(name)
    tick("sw", store, ledger, runtime, now_ms=2000)
    agent = next(agent for agent in store.agents("sw") if agent.task == "t1")
    assert (agent.pane_id, agent.harness, agent.account, agent.model, agent.profile) == (
        "w:p1",
        "claude",
        "acct",
        "opus",
        "engineer",
    )
    assert agent.launch_timings == result.launch_timings
    assert agent.profile_decision == result.profile_decision
    assert agent.model_source == "picker" and agent.choice == "share"
    assert store.claims("sw", "t1") == 1
    assert store.launches("sw")[0]["state"] == "started"
    assert not store.redis.hgetall(store.key("sw", "launch-jobs"))


@pytest.mark.parametrize(
    "result,now",
    [
        ({"error": {"message": "bad launch", "status": "refused", "pane_id": "w:p2", "harness": "claude"}}, 2000),
        (None, 301001),
    ],
)
def test_failed_and_expired_jobs_use_spawn_failure_accounting(store, tmp_path, result, now, monkeypatch):
    monkeypatch.setattr(launch_job, "spawn", lambda job: None)
    from scripts.swarm import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_opened_pane", lambda name, at: "w:p3")
    ledger, runtime = tasks(("t1", "eng")), DetachedRuntime(tmp_path)
    tick("sw", store, ledger, runtime, now_ms=1000)
    name = runtime.jobs[0]
    if result:
        (tmp_path / f"{name}.json").write_text(json.dumps(result))
    store.update("sw", state="paused")
    tick("sw", store, ledger, runtime, now_ms=now)
    assert ledger.rows["t1"]["state"] == "open"
    assert store.claimant("sw", "t1") is None
    assert store.launches("sw")[0]["state"] == "failed"
    assert store.launch_failures("sw")["t1"]
    from scripts.gates import claims

    previous = {"task": "t1", "state": "started", "at": 0}
    assert claims.launch_failure(1, [previous, *store.launches("sw")], "t1") != "none"
    from scripts.swarm import failed_panes

    assert ("w:p2" if result else "w:p3") in store.redis.hgetall(store.key("sw", failed_panes.HELD))
    assert name in runtime.reaped
    assert not store.redis.hgetall(store.key("sw", "launch-jobs"))


def test_job_starts_a_detached_process_and_returns_without_waiting(tmp_path, monkeypatch):
    popen = Mock()
    monkeypatch.setattr(launch_job.subprocess, "Popen", popen)
    config = SwarmConfig("sw", "/repo", 1, 1)
    job = launch_job.start(
        tmp_path,
        config,
        "eng",
        "engineer",
        {
            "task_id": "t1",
            "argv": ["agentihooks", "init-agent"],
            "predecessor": None,
            "controller_epoch": None,
        },
    )
    popen.assert_not_called()
    launch_job.spawn(job)
    args, kwargs = popen.call_args
    assert args[0][1:3] == ["-m", "scripts.swarm.launch_job"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert job["timeout"] == SPAWN_TIMEOUT_S
    assert Path(job["path"]).parent.exists()
    popen.return_value.wait.assert_not_called()


@pytest.mark.parametrize(
    "lane,task,route",
    [
        ("master", {}, []),
        ("eng", {"handoff_envelope": {"reason": "quota"}}, []),
        ("eng", {}, ["--route", "interactive"]),
    ],
)
def test_ordered_launches_remain_synchronous(tmp_path, monkeypatch, lane, task, route):
    runtime = HerdrRuntime(home=tmp_path)
    runtime._launch = Mock(return_value=Placed("w:p1", "claude"))
    runtime._launch.__qualname__ = "_launch"
    detached = Mock()
    monkeypatch.setattr(launch_job, "start", detached)
    config = SwarmConfig("sw", "/repo", 1, 1)
    result = runtime._account_launch(
        config,
        lane,
        {"id": "t1", "detached_launch": True, **task},
        "engineer",
        ["agentihooks", "--agent", "claude", "--", *route],
    )
    assert result.pane_id == "w:p1"
    runtime._launch.assert_called_once()
    detached.assert_not_called()
