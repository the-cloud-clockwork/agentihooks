import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.swarm import launch_job
from scripts.swarm.runtime import SPAWN_TIMEOUT_S, HerdrRuntime
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import Placed, tick
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
        {"id": "t1", "detached_launch": True, "controller_epoch": 7},
        "engineer",
        ["agentihooks", "--agent", "claude", "--", "--route", "account"],
    )
    assert (placed.pane_id, placed.harness) == ("", "claude")
    assert placed.launch_job["timeout"] == 300
    request = json.loads(Path(placed.launch_job["request"]).read_text())
    assert request["launch"]["task_id"] == "t1"
    assert request["launch"]["controller_epoch"] == 7
    assert request["home"] == str(tmp_path)
    assert request["name"] == "engineer" and request["lane"] == "eng"
    runtime._launch.assert_not_called()
    popen.assert_not_called()


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


def test_worker_refuses_to_start_after_its_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_job.time, "time", lambda: 1)
    job = launch_job.start(tmp_path, SwarmConfig("sw", "/repo", 1, 1), "eng", "engineer", {"task_id": "t1", "argv": []})
    monkeypatch.setattr(launch_job.time, "time", lambda: 301)
    launch = Mock()
    monkeypatch.setattr(HerdrRuntime, "_launch", launch)
    launch_job.run(Path(job["request"]))
    launch.assert_not_called()


def test_worker_records_an_unexpected_error_with_spawn_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_job.time, "time", lambda: 1)
    job = launch_job.start(tmp_path, SwarmConfig("sw", "/repo", 1, 1), "eng", "engineer", {"task_id": "t1", "argv": []})
    monkeypatch.setattr(HerdrRuntime, "_launch", Mock(side_effect=RuntimeError("boom")))
    moves, replace_path = [], Path.replace

    def record(self, target):
        moves.append((self.name, Path(target).name))
        return replace_path(self, target)

    monkeypatch.setattr(Path, "replace", record)
    launch_job.run(Path(job["request"]))
    assert moves == [("engineer.writing", "engineer.json")]


def test_worker_gives_the_launcher_no_time_once_the_budget_runs_out_mid_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_job.time, "time", lambda: 1)
    job = launch_job.start(tmp_path, SwarmConfig("sw", "/repo", 1, 1), "eng", "engineer", {"task_id": "t1", "argv": []})
    clock = [2]
    monkeypatch.setattr(launch_job.time, "time", lambda: clock[0])
    calls = []
    monkeypatch.setattr(launch_job.subprocess, "run", lambda argv, **kwargs: calls.append(kwargs["timeout"]))
    monkeypatch.setattr(HerdrRuntime, "reap_name", lambda self, name: None)

    def launch(self, *args, **kwargs):
        clock[0] = 400
        self.run(["launcher"], timeout=300)
        return Placed("w:p1", "claude")

    monkeypatch.setattr(HerdrRuntime, "_launch", launch)
    launch_job.run(Path(job["request"]))
    assert calls == [0]


class DetachedRuntime(FakeRuntime):
    def __init__(self, home):
        super().__init__()
        self.home = home
        self.jobs = []
        self.collected = []

    def launch_room(self, config):
        return 2

    def collect(self, config, lane, name, task):
        self.collected.append(name)
        raise AssertionError("a pending launch job is collected from its result file")

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
    actions = tick("sw", store, ledger, runtime, now_ms=1000)
    assert any(action.startswith("spawn failed for t1") and "cannot start worker" in action for action in actions)
    assert ledger.rows["t1"]["state"] == "open"
    assert not store.redis.hgetall(store.key("sw", launch_job.KEY))
    assert store.launch_failure("sw", "t1") == "cannot start worker"
    assert store.failed_harness("sw", "t1") == "claude"
    assert [row["state"] for row in store.launches("sw")] == ["failed"]


def test_every_placed_task_in_a_pass_starts_its_own_job(store, tmp_path, monkeypatch):
    monkeypatch.setattr(launch_job, "spawn", lambda job: None)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), DetachedRuntime(tmp_path)
    actions = tick("sw", store, ledger, runtime, now_ms=1000)
    assert len(runtime.jobs) == 2
    for name in runtime.jobs:
        task = next(agent.task for agent in store.agents("sw") if agent.name == name)
        assert f"launching {name} for {task}" in actions
    assert sorted(store.redis.hgetall(store.key("sw", launch_job.KEY))) == sorted(runtime.jobs)


def test_pending_launch_keeps_claim_and_is_not_launched_twice(store, tmp_path, monkeypatch):
    monkeypatch.setattr(launch_job, "spawn", lambda job: None)
    ledger, runtime = tasks(("t1", "eng")), DetachedRuntime(tmp_path)
    actions = tick("sw", store, ledger, runtime, now_ms=1000)
    name = ledger.rows["t1"]["claimed_by"]
    assert f"launching {name} for t1" in actions
    assert runtime.jobs == [name]
    assert store.claimant("sw", "t1") == name
    assert store.claims("sw", "t1") == 0
    assert store.launches("sw")[0]["state"] == "launching"
    tick("sw", store, ledger, runtime, now_ms=2000)
    assert runtime.jobs == [name]
    assert runtime.collected == []
    assert store.claimant("sw", "t1") == name
    assert [agent.name for agent in store.agents("sw") if agent.task == "t1"] == [name]


class DecidedRuntime(DetachedRuntime):
    def spawn(self, config: SwarmConfig, lane: str, name: str, task: dict) -> Placed:
        placed = super().spawn(config, lane, name, task)
        return replace(placed, account="acct-a", model="opus", effort="high", profile="engineer")


@pytest.mark.feature("swarm-agent-record-launch-decision-at-spawn")
def test_an_agent_record_read_right_after_a_detached_spawn_carries_its_harness_and_account(
    store: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launch_job, "spawn", lambda job: None)
    ledger, runtime = tasks(("t1", "eng")), DecidedRuntime(tmp_path)
    tick("sw", store, ledger, runtime, now_ms=1000)
    agent = next(agent for agent in store.agents("sw") if agent.task == "t1")
    assert (agent.harness, agent.account, agent.model, agent.effort, agent.profile) == (
        "claude",
        "acct-a",
        "opus",
        "high",
        "engineer",
    )
    assert agent.pane_id == ""


@pytest.mark.feature("swarm-agent-record-launch-decision-at-spawn")
def test_a_detached_spawn_returns_its_routed_account_model_and_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    started = Mock()
    monkeypatch.setattr(launch_job, "spawn", started)

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    runtime._quota_accounts = []
    monkeypatch.setattr(runtime, "_quota_eligible", lambda harness, lane: True)
    monkeypatch.setattr(runtime, "_route_account", lambda *_: (None, SimpleNamespace(name="acct-a")))
    config = SwarmConfig("sw", str(tmp_path), 1, 1, lanes={"eng": {"agent": "claude"}}, code="a1b2c3")
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "detached_launch": True})
    assert placed.launch_job and placed.pane_id == ""
    assert (placed.harness, placed.account) == ("claude", "acct-a")
    assert placed.model and placed.profile
    started.assert_not_called()


@pytest.mark.feature("swarm-agent-record-launch-decision-at-spawn")
def test_a_detached_placement_takes_the_picked_model_and_effort_or_blanks() -> None:
    from scripts.swarm.model_pick import ModelPick
    from scripts.swarm.runtime import _decided

    job = {"path": "/jobs/e.json", "started_at": 1000, "timeout": 300}
    placed = Placed("", "codex", account="acct-a", effort="low", launch_job=job)
    routed = _decided(placed, ["--route", "acct-b"], ModelPick("gpt", "xhigh"), "eng")
    assert (routed.account, routed.model, routed.effort, routed.profile) == ("acct-b", "gpt", "xhigh", "eng")
    unrouted = _decided(placed, [], ModelPick("", ""), "eng")
    assert (unrouted.account, unrouted.model, unrouted.effort) == ("acct-a", "", "")
    synchronous = Placed("w:p1", "claude", effort="low")
    assert _decided(synchronous, ["--route", "acct-c"], ModelPick("opus", "high"), "eng") is synchronous


def test_job_starts_a_detached_process_and_returns_without_waiting(tmp_path, monkeypatch):
    popen = Mock()
    monkeypatch.setattr(launch_job.subprocess, "Popen", popen)
    config = SwarmConfig("sw", "/repo", 1, 1)
    launch = {"task_id": "t1", "argv": ["agentihooks", "init-agent"], "predecessor": None, "controller_epoch": None}
    launch_job.start(tmp_path, config, "eng", "reviewer", launch)
    job = launch_job.start(tmp_path, config, "eng", "engineer", launch)
    assert job["path"] == str(tmp_path / "sw" / launch_job.KEY / "engineer.json")
    popen.assert_not_called()
    launch_job.spawn(job)
    args, kwargs = popen.call_args
    assert args[0] == [sys.executable, "-m", "scripts.swarm.launch_job", job["request"]]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"].name == str(tmp_path / "sw" / launch_job.KEY / "engineer.request.log")
    assert job["timeout"] == SPAWN_TIMEOUT_S
    assert Path(job["path"]).parent.exists()
    popen.return_value.wait.assert_not_called()


@pytest.mark.parametrize(
    "lane,task,route",
    [
        ("master", {}, []),
        ("eng", {"handoff_envelope": {"reason": "quota"}}, []),
        ("eng", {}, ["--route", "interactive", "--model", "opus"]),
        ("eng", {}, ["--route", "master-login", "--model", "opus"]),
        ("eng", {"admitted_execution_id": "e1"}, []),
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
