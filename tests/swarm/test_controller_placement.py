import pytest


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    monkeypatch.setenv("SWARM_HIVE_ID", "anton")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", ".", 2, 1, state="running", max_plan=1))
    return saved


@pytest.fixture(autouse=True)
def engineer_classified(monkeypatch):
    from scripts.swarm import profile_choice

    decision = profile_choice.ProfileDecision("engineer", "classifier", "engineer", "m1", 0.9, True)
    monkeypatch.setattr(profile_choice, "classify", lambda slug, task, environ: decision)


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


def test_distributed_controller_classifies_the_profile_before_it_places_the_task(store, monkeypatch):
    from scripts.swarm import profile_choice
    from tests.swarm.test_tick import FakeLedger

    asked = []

    def classify(slug, task, environ):
        asked.append(task["id"])
        profile = {"page": "frontend", "build": "engineer"}[task["id"]]
        return profile_choice.ProfileDecision(profile, "classifier", profile, "m1", 0.9, True)

    monkeypatch.setattr(profile_choice, "classify", classify)
    runtime = placed_runtime()
    ledger = FakeLedger([{"id": "page", "lane": "eng"}, {"id": "build", "lane": "eng"}])
    tick(store, ledger, runtime)
    tick(store, ledger, runtime)
    assert [task for _, _, task in runtime.spawned] == ["build"]
    assert states(ledger) == {"page": "open", "build": "claimed"}
    assert runtime.tasks[0]["profile_decision"] == {
        "profile": "engineer",
        "source": "classifier",
        "responsibility": "engineer",
        "model": "m1",
        "confidence": 0.9,
        "calibrated": True,
        "anchors": [],
        "overlays": [],
        "bundle_revision": "",
    }
    assert sorted(asked) == ["build", "page"]
    assert store.redis.hkeys(store.key("sw", "profile-decisions")) == ["page"]


def test_a_task_classified_front_end_runs_on_the_workstation_and_an_engineer_task_on_anton(store, monkeypatch):
    from scripts.swarm import profile_choice
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.kubernetes.watch import BACKEND
    from scripts.swarm_v2.runtime.base import Outcome, Status
    from scripts.swarm_v2.runtime.routed import routed
    from tests.swarm.test_tick import FakeLedger, FakeRuntime
    from tests.test_swarm_v2_placement import Remote

    profiles = {"page": "frontend", "build": "engineer"}

    def classify(slug, task, environ):
        profile = profiles[task["id"]]
        return profile_choice.ProfileDecision(profile, "classifier", profile, "m1", 0.9, True)

    launched = []

    def launch(request):
        launched.append((request.task["id"], request.task["profile_decision"]["profile"]))
        return Outcome("spawn", Status.OK, BACKEND, Placed("", "claude", placement=BACKEND))

    monkeypatch.setattr(profile_choice, "classify", classify)
    herdr = FakeRuntime()
    ledger = FakeLedger([{"id": "page"}, {"id": "build"}])
    tick(store, ledger, routed({}, herdr, kubernetes=Remote(), launch=launch))
    assert launched == [("build", "engineer")]
    assert herdr.spawned == []
    assert states(ledger) == {"page": "open", "build": "claimed"}
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    workstation = FakeRuntime()
    tick(store, ledger, workstation)
    assert [(lane, task) for lane, _, task in workstation.spawned] == [("eng", "page")]
    assert states(ledger) == {"page": "claimed", "build": "claimed"}


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


@pytest.mark.parametrize(
    ("machine", "hive_placement", "deployment"),
    [
        ({"AGENTIHOOKS_DEPLOYMENT": "local"}, True, "hybrid"),
        ({}, True, "hybrid"),
        ({"AGENTIHOOKS_DEPLOYMENT": "local"}, False, "local"),
        ({}, False, "local"),
        ({"AGENTIHOOKS_DEPLOYMENT": "distributed"}, True, "distributed"),
        ({"AGENTIHOOKS_DEPLOYMENT": "compose"}, False, "compose"),
    ],
)
def test_hive_placement_turns_only_a_local_machine_swarm_hybrid(store, machine, hive_placement, deployment):
    from scripts.swarm import controller

    config = store.update("sw", hive_placement=hive_placement)
    assert controller.deployment_for(config, machine) == deployment


def test_hive_placement_is_stored_on_the_swarm_and_off_by_default(store):
    assert store.config("sw").hive_placement is False
    assert store.update("sw", hive_placement=True).hive_placement is True
    assert store.config("sw").hive_placement is True
    assert store.update("sw", hive_placement=False).hive_placement is False
    assert store.config("sw").hive_placement is False


@pytest.mark.parametrize(("value", "stored"), [("on", True), ("off", False)])
def test_swarm_set_switches_hive_placement(store, capsys, value, stored):
    import argparse
    import json

    from scripts.swarm import cli

    store.update("sw", state="paused", hive_placement=not stored)
    cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=[f"hive-placement={value}"]))
    assert store.config("sw").hive_placement is stored
    assert json.loads(capsys.readouterr().out)["hive_placement"] is stored


def test_swarm_set_refuses_another_hive_placement_value(store):
    import argparse

    from scripts.swarm import cli
    from scripts.swarm.store import SwarmError

    store.update("sw", state="paused")
    with pytest.raises(SwarmError, match="^hive-placement takes on or off$"):
        cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["hive-placement=yes"]))
    assert store.config("sw").hive_placement is False


def test_placement_mode_is_anton_first_with_no_node_limit_by_default(store):
    config = store.config("sw")
    assert (config.placement_mode, config.placement_nodes) == ("anton-first", 0)
    store.update("sw", placement_mode="round-robin", placement_nodes=3)
    config = store.config("sw")
    assert (config.placement_mode, config.placement_nodes) == ("round-robin", 3)


def test_a_swarm_saved_before_placement_modes_reads_anton_first_with_no_node_limit(store):
    store.redis.hdel(store.key("sw", "config"), "placement_mode", "placement_nodes")
    config = store.config("sw")
    assert (config.placement_mode, config.placement_nodes) == ("anton-first", 0)


@pytest.mark.parametrize("nodes", [-1, "2", 1.5])
def test_swarm_config_refuses_a_node_limit_that_is_not_a_whole_number(nodes):
    from scripts.swarm.store import SwarmConfig, SwarmError

    with pytest.raises(SwarmError, match="^placement-nodes takes a whole number of hives, 0 for no limit$"):
        SwarmConfig("sw", "agentihooks", 1, 1, placement_nodes=nodes)


def test_swarm_set_selects_round_robin_placement_and_its_node_limit(store, capsys):
    import argparse
    import json

    from scripts.swarm import cli

    store.update("sw", state="paused")
    cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["placement-mode=round-robin", "placement-nodes=4"]))
    config = store.config("sw")
    assert (config.placement_mode, config.placement_nodes) == ("round-robin", 4)
    shown = json.loads(capsys.readouterr().out)
    assert (shown["placement_mode"], shown["placement_nodes"]) == ("round-robin", 4)


@pytest.mark.parametrize(
    ("pair", "refusal"),
    [
        ("placement-mode=spread", "^placement mode must be one of anton-first, round-robin$"),
        ("placement-nodes=two", "^placement-nodes takes a whole number of hives, 0 for no limit$"),
        ("placement-nodes=-1", "^placement-nodes takes a whole number of hives, 0 for no limit$"),
    ],
)
def test_swarm_set_refuses_an_unknown_placement_mode_or_node_limit(store, pair, refusal):
    import argparse

    from scripts.swarm import cli
    from scripts.swarm.store import SwarmError

    store.update("sw", state="paused")
    with pytest.raises(SwarmError, match=refusal):
        cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=[pair]))
    config = store.config("sw")
    assert (config.placement_mode, config.placement_nodes) == ("anton-first", 0)


def test_the_tick_of_a_local_machine_swarm_with_hive_placement_runs_hybrid(store, monkeypatch):
    from scripts.swarm import controller
    from tests.swarm.test_tick import FakeLedger

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    store.update("sw", hive_placement=True)
    built = []
    real = controller.FencedRuntime

    def fenced(store, slug, held, runtime, deployment):
        built.append(deployment)
        return real(store, slug, held, runtime, deployment)

    monkeypatch.setattr(controller, "FencedRuntime", fenced)
    tick(store, FakeLedger([]), placed_runtime())
    assert built == ["hybrid"]
