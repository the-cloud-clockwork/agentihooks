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
