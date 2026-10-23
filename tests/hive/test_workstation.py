from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.hive import workstation


def reading():
    return {"cpus": 32, "load5": 0, "available_mb": 60000, "total_mb": 64000}


@pytest.mark.parametrize(
    ("environ", "runs"),
    [({}, True), ({"AGENTIHOOKS_DEPLOYMENT": "local"}, True), ({"AGENTIHOOKS_DEPLOYMENT": "distributed"}, False)],
)
def test_only_the_distributed_deployment_turns_the_workstation_engine_off(environ, runs):
    assert workstation.runs_engine(environ) is runs


def master_event(**data):
    from scripts.events.envelope import Envelope

    fields = {
        "lane": "master",
        "name": "master@rig-1",
        "execution_id": "execution-a",
        "execution_generation": 4,
        "claim_generation": 2,
        **data,
    }
    return Envelope("ready-m", "task.ready", "rig", "task", "master", 0, 12, "hub", fields)


def test_master_work_becomes_a_master_spawn_request_with_its_execution_identity():
    configs = Mock(return_value="rig config")
    request = workstation.MasterTasks(configs).request(master_event())

    configs.assert_called_once_with("rig")
    assert (request.config, request.lane, request.name) == ("rig config", "master", "master@rig-1")
    assert request.task == {
        "id": "master",
        "execution_id": "execution-a",
        "execution_generation": 4,
        "claim_generation": 2,
    }


@pytest.mark.parametrize("missing", ["name", "execution_id", "execution_generation", "claim_generation"])
def test_master_work_without_its_execution_identity_is_withdrawn(missing):
    event = master_event()
    event = type(event).from_dict({**event.to_dict(), "data": {k: v for k, v in event.data.items() if k != missing}})
    configs = Mock()

    assert workstation.MasterTasks(configs).request(event) is None
    configs.assert_not_called()


def test_work_for_another_lane_is_withdrawn():
    assert workstation.MasterTasks(Mock()).request(master_event(lane="eng")) is None


def test_the_workstation_runs_one_master_spoke_per_swarm_as_this_hive():
    store = SimpleNamespace(slugs=lambda: ["a", "b"], config=lambda slug: SimpleNamespace(slug=slug))
    bus, runtime = Mock(), Mock()

    spokes = workstation.spokes(store, bus, runtime, "workstation-1")

    assert [(s.config.swarm.slug, s.config.lane, s.config.hive) for s in spokes] == [
        ("a", "master", "workstation-1"),
        ("b", "master", "workstation-1"),
    ]
    assert all(s.bus is bus and s.runtime is runtime for s in spokes)


def test_a_master_spoke_spawns_offered_master_work_on_the_workstation(monkeypatch):
    from scripts.events.config import BusConfig
    from scripts.events.memory import open_bus
    from scripts.hive import spoke
    from scripts.swarm_v2.runtime.base import Outcome, Status

    monkeypatch.setattr(spoke.Spoke, "_measure", lambda self: reading())
    bus = open_bus(BusConfig())
    bus.queue("swarm.rig.ready.master", spoke.QueuePolicy()).offer(master_event())
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn", Status.OK, "local", SimpleNamespace(pane_id="pane-m", harness="claude", account="slot-a")
    )
    store = SimpleNamespace(slugs=lambda: ["rig"], config=lambda slug: SimpleNamespace(slug=slug))

    assert workstation.run_once(store, bus, runtime, "workstation-1", 0) == ["started"]
    [request] = [call.args[0] for call in runtime.spawn.call_args_list]
    assert (request.lane, request.name, request.task["execution_id"]) == ("master", "master@rig-1", "execution-a")


def test_the_spoke_loop_waits_only_after_a_pass_with_nothing_started(monkeypatch):
    outcomes = iter([["empty", "held"], ["started", "empty"], []])
    seen = []
    monkeypatch.setattr(workstation, "run_once", lambda *args: seen.append(args) or next(outcomes))
    waits = []

    workstation.run("store", "bus", "runtime", "hive", waits.append, passes=range(3))

    assert seen == [("store", "bus", "runtime", "hive", workstation.TAKE_MS)] * 3
    assert waits == [workstation.IDLE_S, workstation.IDLE_S]
