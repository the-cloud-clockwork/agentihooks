import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.swarm import launch_job
from scripts.swarm.runtime import SPAWN_TIMEOUT_S, HerdrRuntime
from scripts.swarm.store import SwarmConfig
from scripts.swarm.tick import Placed, tick
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store


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


def test_pending_launch_keeps_claim_and_is_not_launched_twice(store, tmp_path):
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


def test_completed_job_records_the_existing_placed_fields(store, tmp_path):
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
def test_failed_and_expired_jobs_use_spawn_failure_accounting(store, tmp_path, result, now):
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
