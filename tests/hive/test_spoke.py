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
    from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status

    policy = QueuePolicy(max_deliver=3, backoff_ms=(10, 20))
    clock = SimpleNamespace(now=0)
    bus = open_bus(BusConfig(), clock=lambda: clock.now)
    config = SpokeConfig(SimpleNamespace(slug="rig"), "eng", "hive-a", policy)
    tasks, runtime = Mock(), Mock()
    request = SpawnRequest(config.swarm, "eng", "agent-a", {"id": "task-a"})
    tasks.request.return_value = request
    runtime.spawn.return_value = Outcome(
        "spawn", Status.OK, "local", SimpleNamespace(pane_id="pane-a", harness="claude", account="slot-a")
    )
    event = Envelope(
        "ready-a", "task.ready", "rig", "task", "task-a", 0, 12, "readiness", {"lane": "eng", "generation": 0}
    )
    queue = bus.queue("swarm.rig.ready.eng", policy)
    queue.offer(event)
    spoke = Spoke(config, bus, runtime, tasks, reading)
    yield SimpleNamespace(
        bus=bus,
        queue=queue,
        config=config,
        tasks=tasks,
        runtime=runtime,
        event=event,
        spoke=spoke,
        request=request,
        clock=clock,
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
    assert rig.spoke.poll() == "started"
    assert raced == ["busy"]
    rig.tasks.request.assert_called_once_with(rig.event)
    other_tasks.request.assert_not_called()
    rig.runtime.spawn.assert_called_once_with(rig.request)


def test_a_withdrawn_task_is_terminated_without_launching(rig):
    rig.tasks.request.return_value = None
    assert rig.spoke.poll() == "withdrawn"
    assert rig.queue.take(0) is None
    rig.runtime.spawn.assert_not_called()


def test_a_spoke_spawns_through_the_runtime_then_publishes_started_before_acknowledging(rig):
    from scripts.events.envelope import Envelope

    observed = []
    real_take = rig.queue.take
    real_publish = rig.bus.events.publish

    def take(timeout_ms):
        delivery = real_take(timeout_ms)
        real_ack = delivery.ack

        def ack():
            observed.append("ack")
            real_ack()

        delivery.ack = ack
        return delivery

    def publish(subject, event):
        observed.append((subject, event))
        return real_publish(subject, event)

    rig.queue.take = take
    rig.bus.events.publish = publish
    rig.runtime.spawn.side_effect = lambda request: (observed.append("spawn"), rig.runtime.spawn.return_value)[1]

    assert rig.spoke.poll() == "started"
    rig.runtime.spawn.assert_called_once_with(rig.request)
    assert observed[0] == "spawn"
    subject, event = observed[1]
    assert subject == "swarm.rig.execution.started"
    assert isinstance(event, Envelope)
    assert (event.type, event.swarm, event.entity, event.key, event.version, event.author, event.cause) == (
        "execution.started",
        "rig",
        "execution",
        "agent-a",
        0,
        "hive-a",
        "ready-a",
    )
    assert event.data == {
        "task": "task-a",
        "lane": "eng",
        "hive": "hive-a",
        "name": "agent-a",
        "pane_id": "pane-a",
        "harness": "claude",
        "account": "slot-a",
    }
    assert observed[2:] == ["ack"]
    assert real_take(0) is None


@pytest.mark.parametrize("failure", ["request", "spawn", "outcome"])
def test_failed_work_is_nacked_with_backoff_and_can_be_claimed_on_retry(rig, failure):
    from scripts.swarm_v2.runtime.base import Outcome, Status

    if failure == "request":
        rig.tasks.request.side_effect = [RuntimeError("unavailable"), rig.request]
    elif failure == "spawn":
        rig.runtime.spawn.side_effect = [RuntimeError("unavailable"), rig.runtime.spawn.return_value]
    else:
        rig.runtime.spawn.side_effect = [
            Outcome("spawn", Status.UNAVAILABLE, "local", detail="no room"),
            rig.runtime.spawn.return_value,
        ]
    assert rig.spoke.poll() == "retry"
    assert rig.queue.take(0) is None
    rig.tasks.reopen.assert_not_called()
    rig.clock.now = 9
    assert rig.spoke.poll() == "empty"
    rig.clock.now = 10
    assert rig.spoke.poll() == "started"
    assert rig.runtime.spawn.call_count == (1 if failure == "request" else 2)


def test_a_publication_retry_reuses_the_started_execution_after_a_spoke_restart(rig):
    from scripts.hive.spoke import Spoke

    published = []
    real_publish = rig.bus.events.publish

    def publish(subject, event):
        published.append((subject, event))
        if len(published) == 1:
            raise RuntimeError("publish unavailable")
        return real_publish(subject, event)

    rig.bus.events.publish = publish
    assert rig.spoke.poll() == "retry"
    assert rig.queue.take(0) is None
    rig.clock.now = 10
    restarted = Spoke(rig.config, rig.bus, rig.runtime, rig.tasks, reading)
    assert restarted.poll() == "started"
    rig.runtime.spawn.assert_called_once_with(rig.request)
    rig.tasks.request.assert_called_once_with(rig.event)
    rig.tasks.reopen.assert_not_called()
    assert published[0] == published[1]
    assert rig.queue.take(0) is None
