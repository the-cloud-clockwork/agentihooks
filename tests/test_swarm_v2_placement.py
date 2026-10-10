from types import SimpleNamespace

import pytest

from scripts.swarm.store import MASTER, AgentRecord, SwarmError
from scripts.swarm.tick import Placed, SpawnError
from scripts.swarm_v2.kubernetes.adapter import KubernetesRuntime
from scripts.swarm_v2.kubernetes.watch import BACKEND
from scripts.swarm_v2.runtime.base import (
    LOCAL,
    Capability,
    Outcome,
    Placement,
    Recovery,
    RuntimeRouter,
    SpawnRequest,
    Status,
)
from scripts.swarm_v2.runtime.operations import Operation, OperationConflict, OperationRequest, Phase
from scripts.swarm_v2.runtime.routed import routed

pytestmark = pytest.mark.unit

CONFIG = SimpleNamespace(slug="sw")


class Herdr:
    def __init__(self):
        self.spawned = []

    def spawn(self, config, lane, name, task):
        self.spawned.append(name)
        return Placed(f"pane-{name}", "claude")


class Remote:
    backend = BACKEND
    capabilities = frozenset({Capability.SPAWN})

    def __init__(self):
        self.spawned = []

    def spawn(self, request):
        self.spawned.append(request.name)
        return Outcome("spawn", Status.OK, BACKEND, Placed("", "claude", placement=BACKEND))


def request(lane, name="a1", **task):
    return SpawnRequest(CONFIG, lane, name, {"id": "t1", **task})


@pytest.mark.parametrize(
    ("lane", "task", "backend"),
    [
        ("eng", {}, BACKEND),
        ("ci", {}, BACKEND),
        ("eng", {"profile": "engineer"}, BACKEND),
        ("eng", {"profile": "frontend"}, LOCAL),
        ("ci", {"profile": "frontend"}, LOCAL),
        (MASTER, {}, LOCAL),
        ("plan", {}, LOCAL),
        ("dispatch", {}, LOCAL),
    ],
)
def test_placement_sends_engineer_and_ci_spawns_remote_and_keeps_the_rest_local(lane, task, backend):
    router = RuntimeRouter([Remote()], placement=Placement(BACKEND))
    assert Placement(BACKEND).backend_for(lane, task) == backend
    assert router.spawn_backend(request(lane, **task)) == backend


def test_a_placement_overrides_the_default_backend_for_unplaced_lanes():
    router = RuntimeRouter([Remote()], BACKEND, placement=Placement(BACKEND))
    assert router.spawn_backend(request(MASTER)) == LOCAL
    assert router.spawn_backend() == BACKEND


def test_a_disabled_placement_backend_rolls_placed_spawns_back_to_local():
    router = RuntimeRouter([Remote()], disabled=[BACKEND], placement=Placement(BACKEND))
    assert router.spawn_backend(request("eng")) == LOCAL


def test_the_router_spawns_a_placed_request_on_its_placement_backend():
    remote = Remote()
    outcome = RuntimeRouter([remote], placement=Placement(BACKEND)).spawn(request("eng", "e1"))
    assert outcome == Outcome("spawn", Status.OK, BACKEND, Placed("", "claude", placement=BACKEND))
    assert remote.spawned == ["e1"]


def test_the_tick_runtime_refuses_a_kubernetes_runtime_without_a_distributed_launch():
    with pytest.raises(ValueError) as raised:
        routed({}, Herdr(), kubernetes=Remote())
    assert str(raised.value) == "a Kubernetes runtime needs a distributed launch"


def test_the_tick_runtime_without_kubernetes_keeps_every_spawn_local():
    herdr = Herdr()
    runtime = routed({}, herdr)
    runtime.spawn(CONFIG, "eng", "e1", {"id": "t1"})
    assert herdr.spawned == ["e1"]
    assert list(runtime.router.runtimes) == [LOCAL]
    assert runtime.router.placement is None


def operation(phase):
    return Operation("op-1", "exe-1", 2, "spawn", BACKEND, "d", {}, phase)


class Controller:
    def __init__(self, phase=Phase.APPLIED, error=None):
        self.phase, self.error, self.requests = phase, error, []

    def execute(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return operation(self.phase)


def launch(request):
    return {"task_id": request.task["id"], "harness": "codex", "profile": "general", "execution_id": "forged"}


def admitted(**task):
    return SpawnRequest(CONFIG, "eng", "e1", {"id": "t1", "execution_id": "exe-1", "generation": 2, **task})


def test_the_kubernetes_runtime_spawns_one_journaled_create_for_the_admitted_execution():
    controller = Controller()
    runtime = KubernetesRuntime(controller.execute, launch)
    outcome = runtime.spawn(admitted())
    payload = {"task_id": "t1", "harness": "codex", "profile": "general", "execution_id": "exe-1", "generation": 2}
    assert controller.requests == [OperationRequest("exe-1", 2, "spawn", payload, "create")]
    assert outcome == Outcome("spawn", Status.OK, BACKEND, Placed("", "codex", placement=BACKEND, profile="general"))
    assert runtime.backend == BACKEND
    assert runtime.capabilities == frozenset({Capability.SPAWN})


@pytest.mark.parametrize(
    ("phase", "status"),
    [(Phase.UNKNOWN, Status.AMBIGUOUS), (Phase.REFUSED, Status.REFUSED), (Phase.ACCEPTED, Status.UNAVAILABLE)],
)
def test_the_kubernetes_runtime_reports_an_unapplied_create_without_a_placement(phase, status):
    outcome = KubernetesRuntime(Controller(phase).execute, launch).spawn(admitted())
    assert outcome == Outcome("spawn", status, BACKEND, None, f"spawn operation op-1 is {phase.value}")


@pytest.mark.parametrize("task", [{"execution_id": ""}, {"generation": 0}, {"execution_id": None}])
def test_the_kubernetes_runtime_refuses_a_spawn_without_an_admitted_execution(task):
    controller = Controller()
    outcome = KubernetesRuntime(controller.execute, launch).spawn(admitted(**task))
    assert outcome == Outcome("spawn", Status.REFUSED, BACKEND, None, "no execution identity")
    assert controller.requests == []


@pytest.mark.parametrize("error", [SwarmError("lease lost"), OperationConflict("execution is not admitted")])
def test_the_kubernetes_runtime_refuses_when_the_controller_refuses(error):
    outcome = KubernetesRuntime(Controller(error=error).execute, launch).spawn(admitted())
    assert outcome == Outcome("spawn", Status.REFUSED, BACKEND, None, str(error))


def test_the_kubernetes_runtime_answers_every_other_operation_unsupported():
    runtime, agent = KubernetesRuntime(Controller().execute, launch), AgentRecord("e1", "eng", "t1")
    calls = {
        "observe": lambda: runtime.observe(agent),
        "command": lambda: runtime.command(agent, "go"),
        "drain": lambda: runtime.drain(agent),
        "terminate": lambda: runtime.terminate(agent, ("/home",)),
        "recover": lambda: runtime.recover(agent, Recovery.RESUME, CONFIG, "go"),
    }
    for operation, call in calls.items():
        assert call() == Outcome(operation, Status.UNSUPPORTED, BACKEND, None, f"kubernetes lacks {operation}")


class Launcher:
    def __init__(self, status=Status.OK, detail=""):
        self.status, self.detail, self.spawned = status, detail, []

    def __call__(self, request):
        self.spawned.append(request.name)
        placed = Placed("", "claude", placement=BACKEND) if self.status is Status.OK else None
        return Outcome("spawn", self.status, BACKEND, placed, self.detail)


def test_the_tick_runtime_sends_placed_spawns_through_the_distributed_launch():
    herdr, remote, launcher = Herdr(), Remote(), Launcher()
    runtime = routed({}, herdr, kubernetes=remote, launch=launcher)
    for lane, name, task in (
        ("eng", "e1", {}),
        ("ci", "c1", {}),
        (MASTER, "m1", {}),
        ("plan", "p1", {}),
        ("eng", "f1", {"profile": "frontend"}),
    ):
        placed = runtime.spawn(CONFIG, lane, name, {"id": "t1", **task})
        assert placed.placement == (BACKEND if name in ("e1", "c1") else "")
    assert launcher.spawned == ["e1", "c1"]
    assert remote.spawned == []
    assert herdr.spawned == ["m1", "p1", "f1"]


def test_the_tick_runtime_keeps_placed_spawns_local_when_kubernetes_is_disabled():
    herdr, launcher = Herdr(), Launcher()
    runtime = routed({"AGENTIHOOKS_RUNTIME_DISABLED": BACKEND}, herdr, kubernetes=Remote(), launch=launcher)
    runtime.spawn(CONFIG, "eng", "e1", {"id": "t1"})
    assert (launcher.spawned, herdr.spawned) == ([], ["e1"])


def test_the_tick_raises_a_refused_distributed_launch_as_a_spawn_error():
    runtime = routed({}, Herdr(), kubernetes=Remote(), launch=Launcher(Status.REFUSED, "account_full"))
    with pytest.raises(SpawnError) as raised:
        runtime.spawn(CONFIG, "eng", "e1", {"id": "t1"})
    assert (str(raised.value), raised.value.status) == ("account_full", "refused")
