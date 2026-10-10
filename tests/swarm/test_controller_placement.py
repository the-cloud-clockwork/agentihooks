import pytest

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    monkeypatch.setenv("SWARM_HIVE_ID", "anton")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", ".", 2, 1, state="running", max_plan=1))
    return saved


def placed_runtime():
    from scripts.swarm_v2.runtime.base import Placement, RuntimeRouter
    from tests.swarm.test_tick import FakeRuntime

    runtime = FakeRuntime()
    runtime.router = RuntimeRouter([], placement=Placement("kubernetes"))
    return runtime


def tick(store, ledger, runtime):
    from scripts.swarm import cli
    from tests.inbox.test_wake import FakeHerdr

    return cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}), scheduled=True)


def states(ledger):
    return {task["id"]: task["state"] for task in ledger.state("sw")["tasks"]}


def test_distributed_controller_spawns_engineer_and_ci_work_placed_on_kubernetes(store):
    from tests.swarm.test_tick import FakeLedger

    runtime = placed_runtime()
    ledger = FakeLedger([{"id": "build", "lane": "eng"}, {"id": "gate", "lane": "ci"}])
    actions = tick(store, ledger, runtime)
    assert sorted((lane, task) for lane, _, task in runtime.spawned) == [("ci", "gate"), ("eng", "build")]
    assert all(task["controller_epoch"] == 1 for task in runtime.tasks)
    assert states(ledger) == {"build": "claimed", "gate": "claimed"}
    assert sum(action.startswith("spawned ") for action in actions) == 2


def test_distributed_controller_leaves_workstation_work_unclaimed(store):
    from scripts.swarm import master_start
    from tests.swarm.test_tick import FakeLedger

    runtime = placed_runtime()
    ledger = FakeLedger(
        [
            {"id": "slice", "lane": "plan"},
            {"id": "page", "lane": "eng", "profile": "frontend"},
            {"id": "form", "lane": "eng", "profile": "frontend"},
            {"id": "build"},
        ]
    )
    actions = tick(store, ledger, runtime)
    assert [task for _, _, task in runtime.spawned] == ["build"]
    assert runtime.masters == []
    assert states(ledger) == {"slice": "open", "page": "open", "form": "open", "build": "claimed"}
    assert [store.claimant("sw", task) for task in ("slice", "page", "form")] == [None, None, None]
    assert "the workstation hive spawns master work" in actions
    assert [agent.task for agent in store.agents("sw")] == ["build"]
    assert master_start.read(store, "sw") == {}


def test_seat_placement_refuses_the_dispatcher_and_passes_a_plain_runtime(store):
    from scripts.swarm import controller, dispatch_seat, lease, seat_spawn
    from tests.swarm.test_tick import FakeRuntime

    held = lease.acquire(store, "sw", "anton")
    fenced = controller.FencedRuntime(store, "sw", held, placed_runtime(), "distributed")
    assert seat_spawn.placed_elsewhere(fenced, dispatch_seat.LANE, {}) == (
        f"the workstation hive spawns {dispatch_seat.LANE} work"
    )
    assert seat_spawn.placed_elsewhere(FakeRuntime(), dispatch_seat.LANE, {}) == ""


@pytest.mark.parametrize(
    ("deployment", "router", "refusal"),
    [
        ("compose", True, "controller spawning is disabled in this deployment mode"),
        ("distributed", False, "the distributed controller spawns only through its Kubernetes runtime"),
    ],
)
def test_controller_without_a_kubernetes_placement_spawns_nothing(store, deployment, router, refusal):
    from scripts.swarm import controller, lease
    from scripts.swarm.store import SwarmError
    from tests.swarm.test_tick import FakeRuntime

    held = lease.acquire(store, "sw", "anton")
    inner = placed_runtime() if router else FakeRuntime()
    runtime = controller.FencedRuntime(store, "sw", held, inner, deployment)
    config = store.config("sw")
    assert runtime.has_capacity(config) is False
    assert runtime.placement_refusal("eng", {"id": "t"}) == refusal
    with pytest.raises(SwarmError) as error:
        runtime.spawn(config, "eng", "one", {"id": "t"})
    assert str(error.value) == refusal
    assert inner.spawned == []


def test_local_controller_spawns_every_lane(store):
    from scripts.swarm import controller, lease
    from tests.swarm.test_tick import FakeRuntime

    held = lease.acquire(store, "sw", "anton")
    inner = FakeRuntime()
    runtime = controller.FencedRuntime(store, "sw", held, inner, "local")
    config = store.config("sw")
    assert runtime.has_capacity(config) is True
    inner.full = True
    assert runtime.has_capacity(config) is False
    assert runtime.placement_refusal("plan", {"id": "t"}) == ""
    runtime.spawn(config, "plan", "one", {"id": "t"})
    assert inner.spawned == [("plan", "one", "t")]


def test_distributed_controller_refuses_a_disabled_kubernetes_backend(store):
    from scripts.swarm import controller, lease
    from scripts.swarm_v2.runtime.base import Placement, RuntimeRouter

    held = lease.acquire(store, "sw", "anton")
    inner = placed_runtime()
    inner.router = RuntimeRouter([], disabled=["kubernetes"], placement=Placement("kubernetes"))
    runtime = controller.FencedRuntime(store, "sw", held, inner, "distributed")
    assert runtime.placement_refusal("eng", {"id": "t"}) == ("the kubernetes runtime is disabled on this controller")
    assert runtime.placement_refusal("plan", {"id": "t"}) == "the workstation hive spawns plan work"


def test_distributed_capacity_ignores_the_workstation_session_cap(store):
    from tests.swarm.test_tick import FakeLedger

    runtime = placed_runtime()
    runtime.full = True
    ledger = FakeLedger([{"id": "build"}])
    tick(store, ledger, runtime)
    assert [task for _, _, task in runtime.spawned] == ["build"]
    assert runtime.capacity_for == []


def test_two_ticks_never_spawn_one_task_twice(store):
    from scripts.swarm import commands, controller, lease
    from tests.swarm.test_tick import FakeLedger

    runtime = placed_runtime()
    ledger = FakeLedger([{"id": "build", "lane": "eng"}])
    held = lease.acquire(store, "sw", commands.hive_id())
    token = controller.take_tick_lock(store, "sw", held, 60000)
    assert tick(store, ledger, runtime) == ["another tick is running"]
    assert runtime.spawned == []
    controller.release_tick_lock(store, "sw", token)
    tick(store, ledger, runtime)
    tick(store, ledger, runtime)
    assert [task for _, _, task in runtime.spawned] == ["build"]
    assert states(ledger) == {"build": "claimed"}
