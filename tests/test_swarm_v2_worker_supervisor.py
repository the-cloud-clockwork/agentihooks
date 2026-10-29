import pytest

from scripts.swarm_v2.artifacts.base import ArtifactError
from scripts.swarm_v2.checkpoints import publisher
from scripts.swarm_v2.worker import supervisor
from tests.sv2_ckp04_cases import Clock, LostAck, checkpoint, make, pointers

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path):
    return tmp_path / "durable"


class Capture:
    def __init__(self, clock: Clock, took: float = 1.0, generation: int = 2, execution: str = "e1") -> None:
        self.clock, self.took = clock, took
        self.generation, self.execution = generation, execution
        self.sequences: list[int] = []

    def __call__(self, sequence: int):
        self.sequences.append(sequence)
        self.clock.now += self.took
        return checkpoint(sequence, self.generation, self.execution)


def schedule(root, clock, capture, budget=supervisor.Budget(interval=300, overhead=0.1, drain=10), backend=None):
    return supervisor.Schedule(make(root, capture.execution, capture.generation, backend), capture, budget, clock)


def test_budget_defaults():
    assert supervisor.Budget() == supervisor.Budget(interval=300.0, overhead=0.05, drain=30.0, drain_attempts=3)


def test_periodic_capture_publishes_when_due_and_reports_age(root):
    clock = Clock()
    capture = Capture(clock, took=1.0)
    sched = schedule(root, clock, capture)
    assert sched.metrics() == {"checkpoint_age_seconds": None, "checkpoint_publish_failures": {}}
    assert sched.stopped is False
    first = sched.tick()
    assert (first.promoted, first.reason) == (True, "promoted")
    assert sched.publisher.latest() == "ckpt-g2-s1"
    clock.now = 1299.0
    assert sched.tick() is None
    assert sched.metrics() == {"checkpoint_age_seconds": 298.0, "checkpoint_publish_failures": {}}
    clock.now = 1300.0
    assert sched.tick().promoted
    assert capture.sequences == [1, 2]
    assert sched.publisher.latest() == "ckpt-g2-s2"


def test_a_slow_capture_stretches_the_interval_to_bound_overhead(root):
    clock = Clock()
    capture = Capture(clock, took=60.0)
    sched = schedule(root, clock, capture)
    assert sched.tick().promoted
    clock.now = 1300.0
    assert sched.tick() is None
    clock.now = 1599.0
    assert sched.tick() is None
    clock.now = 1600.0
    assert sched.tick().promoted
    assert capture.sequences == [1, 2]


def test_the_sequence_continues_after_the_pointer(root):
    clock = Clock()
    assert make(root).publish(*checkpoint(4)).promoted
    capture = Capture(clock)
    assert schedule(root, clock, capture).tick().promoted
    assert capture.sequences == [5]


def test_a_lost_acknowledgement_is_counted_and_the_same_checkpoint_replays(root):
    clock = Clock()
    capture = Capture(clock)
    backend = LostAck(root, lose_at=1)
    sched = schedule(root, clock, capture, backend=backend)
    assert sched.tick() is None
    assert sched.metrics()["checkpoint_publish_failures"] == {"interrupted": 1}
    assert sched.publisher.latest() == ""
    clock.now += 300
    again = sched.tick()
    assert again.promoted
    assert capture.sequences == [1]
    assert sched.publisher.latest() == "ckpt-g2-s1"
    clock.now += 300
    assert sched.tick().promoted
    assert capture.sequences == [1, 2]


def test_a_superseded_generation_stops_periodic_promotion(root):
    clock = Clock()
    capture = Capture(clock)
    sched = schedule(root, clock, capture)
    assert sched.tick().promoted
    assert make(root, "e2", 3).publish(*checkpoint(1, 3, "e2")).promoted
    clock.now += 300
    refused = sched.tick()
    assert (refused.promoted, refused.reason) == (False, "superseded_generation")
    assert sched.stopped is True
    clock.now += 300
    assert sched.tick() is None
    assert sched.drain() is None
    assert capture.sequences == [1, 2]
    assert make(root, "e3", 4).latest() == "ckpt-g3-s1"


def test_drain_publishes_off_schedule_within_its_own_budget(root):
    clock = Clock()
    capture = Capture(clock, took=60.0)
    sched = schedule(root, clock, capture)
    assert sched.tick().promoted
    clock.now += 5
    capture.took = 5.0
    drained = sched.drain()
    assert (drained.promoted, drained.reason) == (True, "promoted")
    assert sched.publisher.latest() == "ckpt-g2-s2"


def test_a_drain_capture_past_its_budget_leaves_the_last_checkpoint(root):
    clock = Clock()
    capture = Capture(clock)
    sched = schedule(root, clock, capture)
    assert sched.tick().promoted
    capture.took = 11.0
    drained = sched.drain()
    assert drained == publisher.Publication(False, "drain_budget_exceeded", None)
    assert sched.publisher.latest() == "ckpt-g2-s1"
    assert len(pointers(root)) == 1
    assert sched.metrics()["checkpoint_publish_failures"] == {"drain_budget_exceeded": 1}


def test_a_drain_capture_ending_on_its_deadline_still_publishes(root):
    clock = Clock()
    capture = Capture(clock, took=10.0)
    sched = schedule(root, clock, capture)
    drained = sched.drain()
    assert (drained.promoted, drained.reason) == (True, "promoted")


def test_a_capture_past_the_drain_budget_is_discarded_not_republished(root):
    clock = Clock()
    capture = Capture(clock, took=11.0)
    sched = schedule(root, clock, capture)
    assert sched.drain().reason == "drain_budget_exceeded"
    capture.took = 1.0
    assert sched.tick().promoted
    assert capture.sequences == [1, 2]
    assert make(root).latest() == "ckpt-g2-s2"


def test_an_artifact_error_in_publish_counts_interrupted_and_keeps_the_capture(root):
    clock = Clock()
    capture = Capture(clock)
    sched = schedule(root, clock, capture)
    publish = sched.publisher.publish
    failures = [ArtifactError("local reported a successful write but the read back does not match")]

    def flaky(doc, materials):
        if failures:
            raise failures.pop()
        return publish(doc, materials)

    sched.publisher.publish = flaky
    assert sched.tick() is None
    assert sched.metrics()["checkpoint_publish_failures"] == {"interrupted": 1}
    clock.now += 300
    assert sched.tick().promoted
    assert capture.sequences == [1]


def test_drain_retries_a_lost_acknowledgement_within_its_attempts(root):
    clock = Clock()
    capture = Capture(clock)
    backend = LostAck(root, lose_at=2)
    sched = schedule(root, clock, capture, backend=backend)
    drained = sched.drain()
    assert drained.promoted
    assert capture.sequences == [1]
    assert sched.metrics()["checkpoint_publish_failures"] == {"interrupted": 1}


def test_drain_gives_up_after_its_attempts(root):
    clock = Clock()
    capture = Capture(clock)

    class Failing(LostAck):
        def _mutated(self, name, key):
            raise ConnectionError("acknowledgement lost")

    sched = schedule(root, clock, capture, backend=Failing(root))
    drained = sched.drain()
    assert drained == publisher.Publication(False, "interrupted", None)
    assert sched.metrics()["checkpoint_publish_failures"] == {"interrupted": 3}
    assert capture.sequences == [1]
