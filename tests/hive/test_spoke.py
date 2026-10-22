from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import ANY, Mock

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
    bus.state.return_value.get.return_value = None
    spoke = Spoke(SpokeConfig(SimpleNamespace(slug="rig"), "eng", "hive-a"), bus, runtime, tasks, lambda: measured)

    assert spoke.poll(17) == "held"
    bus.queue.assert_not_called()
    runtime.spawn.assert_not_called()
    tasks.request.assert_not_called()


def test_an_admitted_spoke_takes_only_from_its_own_lane_with_the_requested_timeout():
    from scripts.events.config import QueuePolicy
    from scripts.hive.spoke import Spoke, SpokeConfig

    bus, runtime, tasks = Mock(), Mock(), Mock()
    bus.state.return_value.get.return_value = None
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
    request = SpawnRequest(
        config.swarm, "eng", "agent-a", {"id": "task-a", "execution_id": "execution-a", "execution_generation": 7}
    )
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
    rig.runtime.spawn.assert_called_once_with(replace(rig.request, task={**rig.request.task, "claim_fence": ANY}))


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
    rig.runtime.spawn.assert_called_once_with(replace(rig.request, task={**rig.request.task, "claim_fence": ANY}))
    assert observed[0] == "spawn"
    subject, event = observed[1]
    assert subject == "swarm.rig.execution.started"
    assert isinstance(event, Envelope)
    assert (event.type, event.swarm, event.entity, event.key, event.version, event.author, event.cause) == (
        "execution.started",
        "rig",
        "execution",
        "execution-a",
        7,
        "hive-a",
        "ready-a",
    )
    request = rig.runtime.spawn.call_args.args[0]
    assert event.data == {
        "task": "task-a",
        "claim_key": request.task["claim_fence"]["key"],
        "claim_revision": request.task["claim_fence"]["revision"],
        "generation": 7,
        "lane": "eng",
        "hive": "hive-a",
        "name": "agent-a",
        "pane_id": "pane-a",
        "harness": "claude",
        "account": "slot-a",
    }
    assert observed[2:] == ["ack"]
    assert real_take(0) is None


@pytest.mark.parametrize("failure", ["request", "outcome"])
def test_failed_work_is_nacked_with_backoff_and_can_be_claimed_on_retry(rig, failure):
    from scripts.swarm_v2.runtime.base import Outcome, Status

    if failure == "request":
        rig.tasks.request.side_effect = [RuntimeError("unavailable"), rig.request]
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
    rig.runtime.spawn.assert_called_once_with(replace(rig.request, task={**rig.request.task, "claim_fence": ANY}))
    rig.tasks.request.assert_called_once_with(rig.event)
    rig.tasks.reopen.assert_not_called()
    assert published[0] == published[1]
    assert rig.queue.take(0) is None


def test_the_final_failed_attempt_is_dead_lettered_and_reopened_before_term(rig):
    from dataclasses import replace

    from scripts.swarm_v2.runtime.base import Outcome, Status

    rig.runtime.spawn.return_value = Outcome("spawn", Status.UNAVAILABLE, "local", detail="no room")
    observed = []
    real_publish = rig.bus.dead.publish
    real_take = rig.queue.take

    def publish(subject, event):
        observed.append("dead")
        return real_publish(subject, event)

    def take(timeout_ms):
        delivery = real_take(timeout_ms)
        if delivery is not None:
            real_term = delivery.term

            def term():
                observed.append("term")
                real_term()

            delivery.term = term
        return delivery

    rig.bus.dead.publish = publish
    rig.queue.take = take
    rig.tasks.reopen.side_effect = lambda event: observed.append("reopen")
    assert rig.spoke.poll() == "retry"
    rig.clock.now = 10
    assert rig.spoke.poll() == "retry"
    rig.clock.now = 30
    assert rig.spoke.poll() == "dead"
    assert observed == ["dead", "reopen", "term"]
    rig.tasks.reopen.assert_called_once_with(rig.event)
    letters = rig.bus.dead.consumer("audit", "dead.>").fetch(10, 0)
    assert len(letters) == 1
    letter = letters[0].event
    assert letter.type == "dead_lettered"
    assert letter.cause == rig.event.id
    assert letter.data["event"] == rig.event.to_dict()
    assert letter.data["attempts"] == 3
    assert letter.data["source"] == "swarm.rig.ready.eng"
    letters[0].ack()
    assert real_take(0) is None
    rig.queue.offer(replace(rig.event, id="duplicate-after-dead"))
    assert rig.spoke.poll() == "dead"
    assert rig.runtime.spawn.call_count == 3
    rig.tasks.reopen.assert_called_once()
    assert rig.bus.dead.consumer("audit", "dead.>").fetch(10, 0) == []


def test_the_spoke_claim_and_launch_work_with_the_real_nats_backend(tmp_path):
    from scripts.events import nats
    from scripts.events.config import BusConfig
    from scripts.events.envelope import Envelope
    from scripts.hive.spoke import Spoke, SpokeConfig
    from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status
    from tests.events import nats_server

    with nats_server.running(tmp_path / "nats") as url:
        bus = nats.open_bus(BusConfig(backend="nats", url=url))
        try:
            config = SpokeConfig(SimpleNamespace(slug="rig"), "eng", "hive-a")
            tasks, runtime = Mock(), Mock()
            tasks.request.return_value = SpawnRequest(
                config.swarm,
                "eng",
                "agent-a",
                {"id": "task-a", "execution_id": "execution-a", "execution_generation": 7},
            )
            runtime.spawn.return_value = Outcome(
                "spawn", Status.OK, "local", SimpleNamespace(pane_id="pane-a", harness="claude", account="slot-a")
            )
            queue = bus.queue("swarm.rig.ready.eng", config.policy)
            queue.offer(
                Envelope(
                    "nats-ready",
                    "task.ready",
                    "rig",
                    "task",
                    "task-a",
                    0,
                    12,
                    "readiness",
                    {"lane": "eng", "generation": 0},
                )
            )
            spoke = Spoke(config, bus, runtime, tasks, reading)
            assert spoke.poll(500) == "started"
            runtime.spawn.assert_called_once_with(
                replace(tasks.request.return_value, task={**tasks.request.return_value.task, "claim_fence": ANY})
            )
            found = bus.events.consumer("audit", "swarm.rig.execution.started").fetch(1, 500)
            assert len(found) == 1
            assert found[0].event.key == "execution-a"
            assert found[0].event.version == 7
            assert queue.take(10) is None
        finally:
            bus.close()


@pytest.mark.parametrize("boundary", ["started", "dead", "reopen"])
def test_completion_survives_the_queue_attempt_cap_and_loss_of_admission(rig, boundary):
    from scripts.hive.spoke import Spoke
    from scripts.swarm_v2.runtime.base import Outcome, Status

    if boundary != "started":
        rig.runtime.spawn.return_value = Outcome("spawn", Status.UNAVAILABLE, "local", detail="no room")
    target = rig.bus.events if boundary == "started" else rig.bus.dead
    real_publish = target.publish
    if boundary == "reopen":
        rig.tasks.reopen.side_effect = RuntimeError("ledger unavailable")
    else:
        target.publish = Mock(side_effect=RuntimeError("bus unavailable"))
    for now in (0, 10, 30):
        rig.clock.now = now
        assert rig.spoke.poll() == "retry"
    target.publish = real_publish
    for _ in range(rig.config.policy.max_deliver):
        rig.clock.now += rig.config.policy.ack_wait_ms
        delivery = rig.queue.take(0)
        if delivery is not None:
            delivery.nak(0)
    assert rig.queue.take(0) is None
    rig.tasks.reopen.side_effect = None
    restarted = Spoke(rig.config, rig.bus, rig.runtime, rig.tasks, lambda: None)
    assert restarted.poll() == ("started" if boundary == "started" else "dead")
    if boundary == "started":
        rig.runtime.spawn.assert_called_once_with(replace(rig.request, task={**rig.request.task, "claim_fence": ANY}))
        rig.tasks.reopen.assert_not_called()
    else:
        assert rig.runtime.spawn.call_count == 3
        assert rig.tasks.reopen.call_args.args == (rig.event,)


def test_an_ambiguous_spawn_keeps_its_identity_and_never_launches_again(rig):
    import json

    from scripts.hive.spoke import Spoke, claim_key

    def spawn(request):
        entry = rig.bus.state("spoke-work").get(claim_key(rig.event))
        intent = json.loads(entry.value)
        assert intent["state"] == "launching"
        assert intent["name"] == request.name
        raise RuntimeError("spawn response lost")

    rig.runtime.spawn.side_effect = spawn
    assert rig.spoke.poll() == "retry"
    rig.clock.now = 10
    restarted = Spoke(rig.config, rig.bus, rig.runtime, rig.tasks, reading)
    assert restarted.poll() == "uncertain"
    rig.runtime.spawn.assert_called_once_with(replace(rig.request, task={**rig.request.task, "claim_fence": ANY}))
    rig.tasks.reopen.assert_not_called()


def test_pending_publication_is_recovered_before_taking_new_work_from_a_busy_lane(rig):
    from dataclasses import replace

    real_publish = rig.bus.events.publish
    rig.bus.events.publish = Mock(side_effect=RuntimeError("bus unavailable"))
    assert rig.spoke.poll() == "retry"
    for index in range(4):
        rig.queue.offer(replace(rig.event, id=f"offer-{index}", key=f"new-{index}"))
        rig.clock.now += 10
        assert rig.spoke.poll() == "retry"
    rig.runtime.spawn.assert_called_once_with(replace(rig.request, task={**rig.request.task, "claim_fence": ANY}))
    rig.tasks.reopen.assert_not_called()
    rig.bus.events.publish = real_publish
    assert rig.spoke.poll() == "started"
    assert rig.spoke.poll() == "started"
    assert rig.runtime.spawn.call_count == 2
    rig.tasks.reopen.assert_not_called()


def test_default_admission_reuses_the_previous_cpu_sample_before_taking_again(monkeypatch):
    from scripts.hive import spoke

    first = reading(pod_cpu_usec=10, measured_at=1234)
    busy = reading(pod_cpu_usec=20, measured_at=5678, pod_cpus=1, pod_load=1)
    samples = []

    def measure(pending, now_ms, *, previous=None):
        samples.append((pending, now_ms, previous))
        return first if previous is None else busy

    monkeypatch.setattr(spoke.headroom, "measure", measure)
    monkeypatch.setattr(spoke, "time", SimpleNamespace(time_ns=Mock(side_effect=[1_234_000_000, 5_678_000_000])))
    bus = Mock()
    bus.state.return_value.get.return_value = None
    bus.queue.return_value.take.return_value = None
    worker = spoke.Spoke(spoke.SpokeConfig(SimpleNamespace(slug="rig"), "eng", "hive-a"), bus, Mock(), Mock())
    assert worker.poll() == "empty"
    assert worker.poll() == "held"
    assert samples == [(0, 1234, None), (0, 5678, first)]
    assert all(type(now_ms) is int for _, now_ms, _ in samples)
    bus.queue.assert_called_once_with("swarm.rig.ready.eng", worker.config.policy)


def claim(rig):
    import json

    from scripts.hive.spoke import claim_key

    return json.loads(rig.bus.state("spoke-work").get(claim_key(rig.event)).value)


def seed_pending(rig, state, attempt=1):
    import json

    from scripts.hive.spoke import claim_key

    rig.bus.state("spoke-work").create(claim_key(rig.event), json.dumps(state).encode())
    pending = rig.bus.state("spoke-pending")
    pending.create(rig.spoke._pending_key, json.dumps({"event": rig.event.to_dict(), "attempt": attempt}).encode())
    return pending


@pytest.mark.parametrize("boundary", ["queue", "take"])
def test_one_remaining_admission_slot_takes_without_waiting_and_reports_queue_failures(monkeypatch, boundary):
    from scripts.hive import spoke

    bus = Mock()
    bus.state.return_value.get.return_value = None
    bus.queue.return_value.take.return_value = None
    monkeypatch.setattr(spoke.admission, "room", lambda value: SimpleNamespace(room=1))
    worker = spoke.Spoke(spoke.SpokeConfig(SimpleNamespace(slug="rig"), "eng", "hive-a"), bus, Mock(), Mock(), reading)
    assert worker.poll() == "empty"
    bus.queue.return_value.take.assert_called_once_with(0)
    target = bus.queue if boundary == "queue" else bus.queue.return_value.take
    target.side_effect = RuntimeError("unavailable")
    assert worker.poll() == "retry"


def test_claim_and_pending_ownership_are_visible_before_request_loading(rig):
    from scripts.hive.spoke import Spoke

    seed_pending(rig, {"state": "ready"})
    observed = []
    other = Spoke(rig.config, rig.bus, Mock(), Mock(), Mock(side_effect=AssertionError("must not measure")))

    def request(event):
        observed.append((claim(rig), other.poll()))
        return rig.request

    rig.tasks.request.side_effect = request
    assert rig.spoke.poll() == "started"
    assert observed == [({"owner": "hive-a", "state": "claimed"}, "busy")]


@pytest.mark.parametrize("field", ["hive", "lane", "swarm"])
def test_pending_completion_is_isolated_between_spokes(rig, field):
    from dataclasses import replace

    from scripts.hive.spoke import Spoke
    from scripts.swarm.readiness import queue_name

    real_publish = rig.bus.events.publish

    def publish(subject, event):
        if event.data["task"] == rig.event.key:
            raise RuntimeError("publication unavailable")
        return real_publish(subject, event)

    rig.bus.events.publish = publish
    assert rig.spoke.poll() == "retry"
    value = SimpleNamespace(slug="other") if field == "swarm" else "other"
    config = replace(rig.config, **{field: value})
    ready = replace(rig.event, id="other-ready", key="task-b", swarm=config.swarm.slug)
    rig.bus.queue(queue_name(config.swarm.slug, config.lane), config.policy).offer(ready)
    other = Spoke(config, rig.bus, rig.runtime, rig.tasks, reading)
    assert other.poll() == "started"
    assert rig.runtime.spawn.call_count == 2


def test_competing_claim_creation_is_nacked_for_the_claim_wait(rig):
    from scripts.events.bus import StateConflict

    claims = rig.bus.state("spoke-work")
    real_create = claims.create
    claims.create = Mock(side_effect=StateConflict)
    assert rig.spoke.poll() == "busy"
    rig.tasks.request.assert_not_called()
    assert rig.queue.take(0) is None
    claims.create = real_create
    rig.clock.now = rig.config.policy.ack_wait_ms - 1
    assert rig.spoke.poll() == "empty"
    rig.clock.now += 1
    assert rig.spoke.poll() == "started"


def test_failed_pending_reservation_releases_the_task_claim_for_retry(rig):
    pending = rig.bus.state("spoke-pending")
    real_create = pending.create
    pending.create = Mock(side_effect=RuntimeError("unavailable"))
    assert rig.spoke.poll() == "retry"
    assert claim(rig) == {"state": "ready"}
    rig.runtime.spawn.assert_not_called()
    pending.create = real_create
    rig.clock.now = 10
    assert rig.spoke.poll() == "started"


def test_known_failed_launch_releases_its_pending_reservation_by_revision(rig):
    import json

    pending = rig.bus.state("spoke-pending")
    pending.update = Mock(wraps=pending.update)
    reserved = []

    def request(event):
        reserved.append(pending.get(rig.spoke._pending_key).revision)
        raise RuntimeError("unavailable")

    rig.tasks.request.side_effect = request
    assert rig.spoke.poll() == "retry"
    assert json.loads(pending.get(rig.spoke._pending_key).value) == {}
    pending.update.assert_called_once_with(rig.spoke._pending_key, b"{}", reserved[0])


@pytest.mark.parametrize("attempt", [1, 3])
def test_ready_recovery_retries_below_the_cap_and_reopens_at_the_cap(rig, attempt):
    pending = seed_pending(rig, {"state": "ready"}, attempt)
    rig.spoke.measure = lambda: None
    if attempt == 1:
        assert rig.spoke.poll() == "held"
        rig.tasks.reopen.assert_not_called()
    else:
        assert rig.spoke.poll() == "dead"
        rig.runtime.spawn.assert_not_called()
        rig.tasks.reopen.assert_called_once_with(rig.event)
        letter = rig.bus.dead.consumer("audit", "dead.>").fetch(1, 0)[0].event
        assert letter.data["attempts"] == 3
    assert pending.get(rig.spoke._pending_key).value == b"{}"


def test_uncertain_recovery_retains_ownership_and_does_not_measure_or_take(rig):
    pending = seed_pending(rig, {"state": "launching", "owner": "hive-a", "name": "agent-a"})
    before = pending.get(rig.spoke._pending_key)
    rig.spoke.measure = Mock(side_effect=AssertionError("must not measure"))
    rig.queue.take = Mock(wraps=rig.queue.take)
    assert rig.spoke.poll() == "uncertain"
    assert pending.get(rig.spoke._pending_key) == before
    rig.queue.take.assert_not_called()
    rig.runtime.spawn.assert_not_called()


def test_another_spoke_does_not_restart_an_uncertain_launch(rig):
    import json

    from scripts.hive.spoke import claim_key

    rig.bus.state("spoke-work").create(claim_key(rig.event), json.dumps({"state": "launching"}).encode())
    assert rig.spoke.poll() == "uncertain"
    assert rig.queue.take(0) is None
    rig.clock.now = rig.config.policy.ack_wait_ms - 1
    assert rig.spoke.poll() == "empty"
    rig.runtime.spawn.assert_not_called()


def test_another_spoke_finishes_a_pending_dead_letter_without_a_local_pending_record(rig):
    import json

    from scripts.events.dead import dead_letter
    from scripts.hive.spoke import claim_key

    letter = dead_letter(rig.event, "swarm.rig.ready.eng", 3)
    rig.bus.state("spoke-work").create(
        claim_key(rig.event), json.dumps({"state": "dead_pending", "event": letter.to_dict()}).encode()
    )
    assert rig.spoke.poll() == "dead"
    rig.tasks.reopen.assert_called_once_with(rig.event)
    rig.runtime.spawn.assert_not_called()
    assert rig.queue.take(0) is None


def test_completed_dead_work_does_not_repeat_recovery_on_a_held_spoke(rig):
    from scripts.swarm_v2.runtime.base import Outcome, Status

    rig.runtime.spawn.return_value = Outcome("spawn", Status.UNAVAILABLE, "local")
    for now in (0, 10):
        rig.clock.now = now
        assert rig.spoke.poll() == "retry"
    rig.clock.now = 29
    assert rig.spoke.poll() == "empty"
    rig.clock.now = 30
    assert rig.spoke.poll() == "dead"
    rig.spoke.measure = lambda: None
    assert rig.spoke.poll() == "held"
    rig.tasks.reopen.assert_called_once_with(rig.event)


def test_ambiguous_launch_intent_keeps_owner_and_name_after_the_exception(rig):
    rig.runtime.spawn.side_effect = RuntimeError("response lost")
    assert rig.spoke.poll() == "retry"
    assert claim(rig) == {"state": "launching", "owner": "hive-a", "name": "agent-a"}


def test_started_event_uses_a_millisecond_wall_clock(rig, monkeypatch):
    from scripts.hive import spoke

    monkeypatch.setattr(spoke, "time", SimpleNamespace(time_ns=lambda: 1_234_567_890_123))
    assert rig.spoke.poll() == "started"
    event = rig.bus.events.consumer("audit", "swarm.rig.execution.started").fetch(1, 0)[0].event
    assert event.time == 1_234_567


def test_withdrawn_work_retains_its_terminal_state_and_cannot_be_loaded_again(rig):
    from dataclasses import replace

    rig.tasks.request.return_value = None
    assert rig.spoke.poll() == "withdrawn"
    assert claim(rig) == {"state": "withdrawn"}
    rig.queue.offer(replace(rig.event, id="withdrawn-duplicate"))
    assert rig.spoke.poll() == "withdrawn"
    rig.tasks.request.assert_called_once_with(rig.event)


def test_spawned_agent_keeps_the_launch_claim_revision_after_started_publication(rig):
    from scripts.hive.spoke import claim_key

    assert rig.spoke.poll() == "started"
    request = rig.runtime.spawn.call_args.args[0]
    entry = rig.bus.state("spoke-work").get(claim_key(rig.event))
    import json

    state = json.loads(entry.value)
    assert request.task["claim_fence"] == {"key": claim_key(rig.event), "revision": state["claim_revision"]}
    assert entry.revision > state["claim_revision"]


@pytest.mark.parametrize("state", ["dead", "exited"])
def test_hive_observations_end_the_same_execution_the_spoke_started(rig, state):
    from scripts.hive.execution_events import publish

    assert rig.spoke.poll() == "started"
    request = rig.runtime.spawn.call_args.args[0]
    report = {
        "observed_at": 1234,
        "executions": {
            "registered-execution": {
                "slug": "rig",
                "name": request.name,
                "generation": rig.request.task["execution_generation"],
                "pane": {"state": "working"},
            }
        },
    }
    publish(rig.bus, "hive-a", report)
    report["executions"]["registered-execution"]["pane"]["state"] = state
    publish(rig.bus, "hive-a", report)
    events = rig.bus.events.consumer("audit", "swarm.rig.execution.*").fetch(10, 0)
    assert [item.event.type for item in events] == ["execution.started", "execution.exited"]
    started, exited = [item.event for item in events]
    assert (exited.key, exited.version, exited.cause) == (started.key, started.version, started.id)
    assert exited.data == started.data
    assert exited.data["task_generation"] == rig.event.version
    assert exited.data["generation"] == rig.request.task["execution_generation"]
    assert {key: exited.data[key] for key in ("claim_key", "claim_revision")} == {
        "claim_key": request.task["claim_fence"]["key"],
        "claim_revision": request.task["claim_fence"]["revision"],
    }


def test_a_heartbeat_during_spawn_waits_for_the_canonical_started_envelope(rig):
    from scripts.hive.execution_events import publish

    outcome = rig.runtime.spawn.return_value
    report = {
        "observed_at": 1234,
        "executions": {
            "registered-execution": {
                "slug": "rig",
                "name": rig.request.name,
                "generation": rig.request.task["execution_generation"],
                "pane": {"state": "working"},
            }
        },
    }

    def spawn(request):
        publish(rig.bus, "hive-a", report)
        assert rig.bus.events.consumer("during-spawn", "swarm.rig.execution.*").fetch(10, 0) == []
        return outcome

    rig.runtime.spawn.side_effect = spawn
    assert rig.spoke.poll() == "started"
    publish(rig.bus, "hive-a", report)
    events = rig.bus.events.consumer("audit", "swarm.rig.execution.*").fetch(10, 0)
    assert len(events) == 1
    assert events[0].event.data["claim_revision"] == rig.runtime.spawn.call_args.args[0].task["claim_fence"]["revision"]


def test_a_request_without_admitted_execution_identity_is_retried_without_spawning(rig):
    rig.tasks.request.return_value = replace(rig.request, task={"id": "task-a"})
    assert rig.spoke.poll() == "retry"
    rig.runtime.spawn.assert_not_called()
    assert claim(rig) == {"state": "ready"}
