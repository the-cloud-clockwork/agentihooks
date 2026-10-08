import subprocess
from unittest.mock import Mock

import pytest

from scripts.swarm import cli, command_runner, commands
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.inbox.test_wake import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", ".", 0, 0, state="paused"))
    return saved


def test_tick_executes_queued_control_then_publishes_its_result(store, monkeypatch):
    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    calls = []

    def run(argv, env):
        calls.append((argv, env))
        store.update("sw", max_eng=2)
        return ""

    monkeypatch.setattr(command_runner, "run", run)
    commands.submit(store, "sw", "swarm", ["set", "max-eng-agents=2"])
    result = cli.run_tick(store, "sw", FakeLedger([]), FakeRuntime(), FakeHerdr({}))
    assert "control swarm acknowledged" in result
    assert calls[0][0] == ["swarm", "sw", "set", "max-eng-agents=2"]
    assert calls[0][1]["AGENTIHOOKS_AGENT_NAME"] == "operator"
    assert calls[0][1]["AGENTIHOOKS_CONTROL_SOURCE"] == "page"
    assert commands.view(store, "sw")["config"]["max_eng"] == 2
    assert commands.rows(store, "sw")[0]["state"] == "acknowledged"
    cli.run_tick(store, "sw", FakeLedger([]), FakeRuntime(), FakeHerdr({}))
    assert len(calls) == 1


def test_other_hive_cannot_tick_or_accept_commands(store, monkeypatch):
    commands.bind(store, "sw", "home")
    monkeypatch.setenv("SWARM_HIVE_ID", "other")
    commands.submit(store, "sw", "swarm", ["pause"])
    assert cli.run_tick(store, "sw") == ["the swarm belongs to another hive"]
    assert commands.rows(store, "sw")[0]["state"] == "pending"


def test_termination_still_requires_membership_and_successful_dry_run(store, monkeypatch):
    run = Mock(return_value="")
    monkeypatch.setattr(command_runner, "run", run)
    row = {"command": "swarm", "argv": ["terminate", "a"]}
    assert command_runner.execute(store, "sw", row) == "agent is not in this swarm"
    run.assert_not_called()
    store.put_agent("sw", AgentRecord("a", "eng", "t"))
    assert command_runner.execute(store, "sw", row) == ""
    assert [call.args[0] for call in run.call_args_list] == [
        ["terminate-agent", "a", "--type", "any", "--dry-run"],
        ["terminate-agent", "a", "--type", "any"],
    ]
    run.reset_mock()
    run.return_value = "dry run refused"
    assert command_runner.execute(store, "sw", row) == "dry run refused"
    assert run.call_count == 1


def test_doctor_and_unknown_command_results(store, monkeypatch):
    run = Mock(return_value="doctor failed")
    monkeypatch.setattr(command_runner, "run", run)
    assert command_runner.execute(store, "sw", {"command": "doctor", "argv": ["stop"]}) == "doctor failed"
    assert run.call_args.args[0] == ["doctor", "sw", "stop"]
    run.reset_mock()
    assert command_runner.execute(store, "sw", {"command": "wrong", "argv": []}) == "unknown control command"
    run.assert_not_called()


def test_quota_refresh_is_executed_on_the_hive(store, monkeypatch):
    from scripts import agents_quota

    monkeypatch.setattr(agents_quota, "refresh_page_quota", lambda probe: probe())
    run = Mock(return_value="quota failed")
    monkeypatch.setattr(command_runner, "run", run)
    assert command_runner.execute(store, "sw", {"command": "quota", "argv": []}) == "quota failed"
    assert run.call_args.args[0] == ["quota", "--refresh", "--json"]


def test_tool_failure_is_acknowledged_without_retrying(store, monkeypatch):
    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    monkeypatch.setattr(command_runner.shutil, "which", lambda name: "/missing/agentihooks")
    failed = Mock(side_effect=subprocess.TimeoutExpired(["agentihooks"], 60))
    monkeypatch.setattr(command_runner.subprocess, "run", failed)
    commands.submit(store, "sw", "swarm", ["pause"])
    assert command_runner.consume(store, "sw") == ["control swarm failed"]
    assert commands.rows(store, "sw")[0]["error"] == "Command '['agentihooks']' timed out after 60 seconds"
    assert command_runner.consume(store, "sw") == []
    assert failed.call_count == 1


def test_invalid_plan_metadata_does_not_fail_a_completed_tick(store):
    ledger = FakeLedger([{"id": "t", "depends_on": ["missing"]}])
    cli.run_tick(store, "sw", ledger, FakeRuntime(), FakeHerdr({}))
    view = commands.view(store, "sw")
    assert view["plan_shape"] == {"error": "plan has unknown dependencies: missing"}
    assert view["tasks"]["open"] == 1


def test_worker_tool_deadlines_remain_specific_to_quota(monkeypatch):
    monkeypatch.setattr(command_runner.shutil, "which", lambda name: "/tools/agentihooks")
    calls = []

    def tool(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(command_runner.subprocess, "run", tool)
    assert command_runner.run(["swarm", "sw", "pause"], {}) == ""
    assert command_runner.run(["quota", "--refresh", "--json"], {}) == ""
    assert calls == [
        (["/tools/agentihooks", "swarm", "sw", "pause"], 60),
        (["/tools/agentihooks", "quota", "--refresh", "--json"], 120),
    ]


def test_quota_and_termination_run_with_operator_identity(store, monkeypatch):
    from scripts import agents_quota

    monkeypatch.setattr(agents_quota, "refresh_page_quota", lambda probe: probe())
    store.put_agent("sw", AgentRecord("a", "eng", "t"))
    seen = []

    def run(argv, env):
        identity, source = env.get("AGENTIHOOKS_AGENT_NAME"), env.get("AGENTIHOOKS_CONTROL_SOURCE")
        assert identity == "operator"
        assert source == "page"
        seen.append(argv)
        return ""

    monkeypatch.setattr(command_runner, "run", run)
    assert command_runner.execute(store, "sw", {"command": "quota", "argv": []}) == ""
    commands.submit(store, "sw", "swarm", ["terminate", "a"])
    assert command_runner.consume(store, "sw") == ["control swarm acknowledged"]
    assert seen == [
        ["quota", "--refresh", "--json"],
        ["terminate-agent", "a", "--type", "any", "--dry-run"],
        ["terminate-agent", "a", "--type", "any"],
    ]


def test_hive_can_publish_an_empty_ledger(store):
    command_runner.publish(store, "sw", {})
    assert commands.workspaces(store, "sw") == {}
    assert commands.view(store, "sw")["tasks"] == {"open": 0, "claimed": 0, "blocked": 0, "pr": 0, "done": 0}


def test_commands_carry_epoch_and_refuse_stale_submission(store):
    from scripts.swarm import lease
    from scripts.swarm.store import SwarmError

    held = lease.acquire(store, "sw", commands.hive_id())
    sent = commands.submit(store, "sw", "swarm", ["pause"])
    assert sent["epoch"] == held.epoch
    assert lease.release(store, "sw", held)
    lease.acquire(store, "sw", commands.hive_id())
    with pytest.raises(SwarmError) as error:
        commands.submit(store, "sw", "swarm", ["pause"], epoch=held.epoch)
    assert str(error.value) == "the controller lease is stale"
    calls = []
    assert commands.consume(store, "sw", commands.hive_id(), lambda row: calls.append(row) or "") == [
        "control swarm failed"
    ]
    assert calls == []
    assert commands.rows(store, "sw")[0]["error"] == "the controller lease is stale"


@pytest.mark.parametrize("mode", ["compose", "distributed"])
def test_controller_nonlocal_runs_tick_without_spawning(store, monkeypatch, mode):
    from scripts.swarm import controller, lease

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", mode)
    runtime = FakeRuntime()
    ledger = FakeLedger([{"id": "task", "title": "Work", "state": "open", "phase": ""}])
    store.update("sw", state="running", max_eng=1)
    result = controller.run_once(store, ledger=ledger, runtime=runtime, messenger=FakeHerdr({}))
    assert list(result) == ["sw"]
    assert lease.current(store, "sw").owner == commands.hive_id()
    assert runtime.spawned == []
    assert runtime.masters == []
    assert ledger.state("sw")["tasks"][0]["state"] == "open"


def test_controller_fences_ledger_and_spawn_writes(store):
    from scripts.swarm import controller, lease
    from scripts.swarm.store import SwarmError

    runtime = FakeRuntime()
    ledger = FakeLedger([])
    held = lease.acquire(store, "sw", "home")
    guarded_ledger = controller.FencedLedger(store, "sw", held, ledger)
    guarded_runtime = controller.FencedRuntime(store, "sw", held, runtime, True)
    assert guarded_ledger.state("sw") == ledger.state("sw")
    guarded_runtime.spawn(store.config("sw"), "eng", "one", {"id": "t"})
    assert runtime.tasks[0]["controller_epoch"] == 1
    assert lease.release(store, "sw", held)
    lease.acquire(store, "sw", "other")
    for write in (
        lambda: guarded_ledger.update_task("sw", "t", {"state": "claimed"}),
        lambda: guarded_runtime.spawn(store.config("sw"), "eng", "two", {"id": "t"}),
    ):
        with pytest.raises(SwarmError) as error:
            write()
        assert str(error.value) == "the controller lease is stale"
    assert len(runtime.spawned) == 1
