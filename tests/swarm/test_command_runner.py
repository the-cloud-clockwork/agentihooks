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


def test_spawn_receiver_refuses_epoch_after_launch_preparation(store, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from scripts.swarm import lease, runtime
    from scripts.swarm.store import SwarmError

    held = lease.acquire(store, "sw", "home")
    calls = []
    monkeypatch.setattr(runtime, "connect", lambda: store, raising=False)
    receiver = runtime.HerdrRuntime(home=tmp_path, run=lambda *args, **kwargs: calls.append(args))
    assert lease.release(store, "sw", held)
    lease.acquire(store, "sw", "other")
    with pytest.raises(SwarmError) as error:
        receiver._launch(SimpleNamespace(slug="sw"), "eng", "t", "one", ["--agent", "claude"], controller_epoch=1)
    assert str(error.value) == "the controller lease is stale"
    assert calls == []


def test_new_epoch_can_tick_while_the_old_tick_is_still_locked(store, monkeypatch):
    from scripts.swarm import lease

    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    held = lease.acquire(store, "sw", "home")
    store.redis.set(store.key("sw", "tick-lock"), '{"epoch": 1, "token": "old tick"}', px=600000)
    assert cli.run_tick(store, "sw", FakeLedger([]), FakeRuntime(), FakeHerdr({})) == ["another tick is running"]
    assert lease.release(store, "sw", held)
    monkeypatch.setenv("SWARM_HIVE_ID", "other")
    result = cli.run_tick(store, "sw", FakeLedger([]), FakeRuntime(), FakeHerdr({}))
    assert "another tick is running" not in result
    assert lease.current(store, "sw").epoch == 2
    assert lease.current(store, "sw").owner == "other"
    assert store.redis.get(store.key("sw", "tick-lock")) is None


def test_old_tick_cannot_release_a_new_epochs_lock(store):
    import json

    from scripts.swarm import controller, lease

    first = lease.acquire(store, "sw", "home")
    old = controller.take_tick_lock(store, "sw", first, 10000)
    assert json.loads(old)["epoch"] == 1
    assert store.redis.pttl(store.key("sw", "tick-lock")) > 9000
    assert lease.release(store, "sw", first)
    second = lease.acquire(store, "sw", "other")
    new = controller.take_tick_lock(store, "sw", second, 10000)
    assert json.loads(new)["epoch"] == 2
    assert old != new
    controller.release_tick_lock(store, "sw", old)
    assert store.redis.get(store.key("sw", "tick-lock")) == new
    controller.release_tick_lock(store, "sw", new)
    assert store.redis.get(store.key("sw", "tick-lock")) is None


def test_tick_lock_conflicts_fail_closed(store, monkeypatch):
    from redis.exceptions import WatchError

    from scripts.swarm import controller, lease

    held = lease.acquire(store, "sw", "home")
    token = controller.take_tick_lock(store, "sw", held, 10000)
    pipeline = store.redis.pipeline

    def conflicting_pipeline():
        pipe = pipeline()
        pipe.execute = lambda: (_ for _ in ()).throw(WatchError("conflict"))
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", conflicting_pipeline)
    controller.release_tick_lock(store, "sw", token)
    assert store.redis.get(store.key("sw", "tick-lock")) == token
    assert lease.release(store, "sw", held) is False
    store.redis.delete(store.key("sw", "tick-lock"))
    assert controller.take_tick_lock(store, "sw", held, 10000) is None


def test_bind_and_explicit_command_epoch_and_unbound_command(store):
    from scripts.swarm import lease

    unbound = commands.submit(store, "sw", "swarm", ["pause"])
    assert unbound["epoch"] == 0
    assert commands.bind(store, "sw", "home") is True
    assert commands.bind(store, "sw", "other") is False
    held = lease.current(store, "sw")
    sent = commands.submit(store, "sw", "swarm", ["pause"], epoch=held.epoch)
    assert sent["epoch"] == 1


def test_legacy_queued_command_is_stamped_with_current_epoch(store):
    import json

    from scripts.swarm import lease

    held = lease.acquire(store, "sw", "home")
    sent = commands.submit(store, "sw", "swarm", ["pause"])
    row = dict(sent)
    del row["epoch"]
    store.redis.hset(store.key("sw", "commands"), sent["id"], json.dumps(row))
    assert lease.release(store, "sw", held)
    current = lease.acquire(store, "sw", "home")
    assert current.epoch == 2
    seen = []
    assert commands.consume(store, "sw", "home", lambda record: seen.append(dict(record)) or "") == [
        "control swarm acknowledged"
    ]
    assert seen[0]["epoch"] == 2
    assert seen[0]["state"] == "accepted"


def test_fenced_adapters_preserve_arguments_and_refuse_disabled_spawn(store):
    from types import SimpleNamespace

    from scripts.swarm import controller, lease
    from scripts.swarm.store import SwarmError

    held = lease.acquire(store, "sw", "home")
    calls = []
    ledger = SimpleNamespace(update_task=lambda *args, **kwargs: calls.append((args, kwargs)) or "accepted")
    guarded = controller.FencedLedger(store, "sw", held, ledger)
    assert guarded.update_task("sw", "t", {"state": "claimed"}, if_state=("open",)) == "accepted"
    assert calls == [(("sw", "t", {"state": "claimed"}), {"if_state": ("open",)})]
    config = store.config("sw")
    receiver = SimpleNamespace(
        has_capacity=lambda saved: False,
        spawn=lambda *args: calls.append(args) or "placed",
    )
    enabled = controller.FencedRuntime(store, "sw", held, receiver, True)
    assert enabled.has_capacity(config) is False
    assert enabled.spawn(config, "eng", "one", {"id": "t"}) == "placed"
    assert calls[-1] == (config, "eng", "one", {"id": "t", "controller_epoch": 1})
    disabled = controller.FencedRuntime(store, "sw", held, receiver, False)
    with pytest.raises(SwarmError) as error:
        disabled.spawn(config, "eng", "one", {"id": "t"})
    assert str(error.value) == "controller spawning is disabled in this deployment mode"


@pytest.mark.parametrize("operand", ["tick-lock", "control-owner"])
def test_tick_lock_detects_writes_interleaved_with_acquisition(store, monkeypatch, operand):
    import json

    from scripts.swarm import controller, lease

    held = lease.acquire(store, "sw", "home")
    pipeline = store.redis.pipeline
    value = json.dumps(
        {"epoch": 1, "token": "other tick"}
        if operand == "tick-lock"
        else {"owner": "other", "epoch": 2, "expires_at": held.expires_at}
    )

    def interleaved_pipeline():
        pipe = pipeline()
        execute = pipe.execute

        def commit():
            store.redis.set(store.key("sw", operand), value, px=180000)
            return execute()

        pipe.execute = commit
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", interleaved_pipeline)
    assert controller.take_tick_lock(store, "sw", held, 10000) is None
    assert store.redis.get(store.key("sw", operand)) == value


def test_run_once_passes_the_supplied_tick_interfaces(store, monkeypatch):
    from scripts.swarm import controller

    ledger, runtime, messenger = object(), object(), object()
    calls = []
    monkeypatch.setattr(cli, "run_tick", lambda *args, **kwargs: calls.append((args, kwargs)) or ["done"])
    assert controller.run_once(store, ledger, runtime, messenger) == {"sw": ["done"]}
    assert calls == [((store, "sw", ledger, runtime, messenger), {"scheduled": True})]


def test_runtime_spawn_carries_epoch_to_launch_receiver(store, monkeypatch, tmp_path):
    from scripts.swarm import runtime
    from scripts.swarm.store import SwarmError

    receiver = runtime.HerdrRuntime(home=tmp_path, choose=lambda *_: ("claude", "open"))
    config = store.update("sw", repo=str(tmp_path))
    captured = []

    def launch(*args, **kwargs):
        captured.append(kwargs)
        raise SwarmError("launch reached")

    monkeypatch.setattr(receiver, "_launch", launch)
    with pytest.raises(SwarmError) as error:
        receiver.spawn(config, "eng", "one", {"id": "t", "title": "Work", "controller_epoch": 7})
    assert str(error.value) == "launch reached"
    assert captured[0]["controller_epoch"] == 7


def test_launch_receiver_accepts_the_current_epoch(store, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from scripts.swarm import lease, runtime
    from scripts.swarm.store import SwarmError

    held = lease.acquire(store, "sw", "home")
    monkeypatch.setattr(runtime, "connect", lambda: store)

    def launch(*args, **kwargs):
        raise SwarmError("launch reached")

    receiver = runtime.HerdrRuntime(home=tmp_path, run=launch)
    config = SimpleNamespace(slug="sw", autonomy="delegate", compact_limit=0)
    with pytest.raises(SwarmError) as error:
        receiver._launch(config, "eng", "t", "one", ["--agent", "claude"], controller_epoch=held.epoch)
    assert str(error.value) == "launch reached"
