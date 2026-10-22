from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def reading(**changes):
    return {"cpus": 32, "load5": 0, "available_mb": 60000, "total_mb": 64000, **changes}


@pytest.mark.parametrize(
    "measured",
    [None, reading(pod_cpus=1, pod_load=1), reading(pod_available_mb=0, pod_total_mb=4096)],
)
def test_a_spoke_does_not_take_work_until_its_local_cgroup_admits_another_agent(measured):
    from scripts.hive.spoke import Spoke, SpokeConfig

    bus, runtime, tasks = Mock(), Mock(), Mock()
    spoke = Spoke(SpokeConfig(SimpleNamespace(slug="rig"), "eng", "hive-a"), bus, runtime, tasks, lambda: measured)

    assert spoke.poll(17) == "held"
    bus.queue.assert_not_called()
    runtime.spawn.assert_not_called()
    tasks.request.assert_not_called()


def test_an_admitted_spoke_takes_only_from_its_own_lane_with_the_requested_timeout():
    from scripts.events.config import QueuePolicy
    from scripts.hive.spoke import Spoke, SpokeConfig

    bus, runtime, tasks = Mock(), Mock(), Mock()
    bus.queue.return_value.take.return_value = None
    policy = QueuePolicy(max_deliver=3, backoff_ms=(10, 20))
    spoke = Spoke(SpokeConfig(SimpleNamespace(slug="rig"), "ci", "hive-a", policy), bus, runtime, tasks, reading)

    assert spoke.poll(23) == "empty"
    bus.queue.assert_called_once_with("swarm.rig.ready.ci", policy)
    bus.queue.return_value.take.assert_called_once_with(23)
    runtime.spawn.assert_not_called()


@pytest.fixture
def rig():
    from scripts.events.config import BusConfig, QueuePolicy
    from scripts.events.envelope import Envelope
    from scripts.events.memory import open_bus
    from scripts.hive.spoke import Spoke, SpokeConfig
    from scripts.swarm_v2.runtime.base import SpawnRequest

    policy = QueuePolicy(max_deliver=3, backoff_ms=(10, 20))
    bus = open_bus(BusConfig())
    config = SpokeConfig(SimpleNamespace(slug="rig"), "eng", "hive-a", policy)
    tasks, runtime = Mock(), Mock()
    request = SpawnRequest(config.swarm, "eng", "agent-a", {"id": "task-a"})
    tasks.request.return_value = request
    event = Envelope(
        "ready-a", "task.ready", "rig", "task", "task-a", 0, 12, "readiness", {"lane": "eng", "generation": 0}
    )
    queue = bus.queue("swarm.rig.ready.eng", policy)
    queue.offer(event)
    spoke = Spoke(config, bus, runtime, tasks, reading)
    yield SimpleNamespace(
        bus=bus, queue=queue, config=config, tasks=tasks, runtime=runtime, event=event, spoke=spoke, request=request
    )
    bus.close()


def test_only_one_spoke_can_claim_duplicate_offers_of_the_same_task_generation(rig):
    from dataclasses import replace

    from scripts.hive.spoke import Spoke

    rig.queue.offer(replace(rig.event, id="duplicate-a"))
    other_tasks = Mock()
    other = Spoke(replace(rig.config, hive="hive-b"), rig.bus, Mock(), other_tasks, reading)
    raced = []

    def request(event):
        raced.append(other.poll())
        return rig.request

    rig.tasks.request.side_effect = request
    assert rig.spoke.poll() == "claimed"
    assert raced == ["busy"]
    rig.tasks.request.assert_called_once_with(rig.event)
    other_tasks.request.assert_not_called()
    rig.runtime.spawn.assert_not_called()


def test_a_withdrawn_task_is_terminated_without_launching(rig):
    rig.tasks.request.return_value = None
    assert rig.spoke.poll() == "withdrawn"
    assert rig.queue.take(0) is None
    rig.runtime.spawn.assert_not_called()
