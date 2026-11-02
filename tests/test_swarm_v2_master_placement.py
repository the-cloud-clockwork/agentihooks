import json
import os
import sys
from pathlib import Path

import pytest

from hooks.context.profile_chain import PACKAGE_ROLES
from scripts.swarm import controller
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2 import deployed, master_fill, worker_home
from scripts.swarm_v2.kubernetes import spec
from scripts.swarm_v2.masters import MasterSeats
from scripts.swarm_v2.runtime.base import LOCAL
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from tests.swarm.test_tick import FakeRuntime
from tests.test_swarm_v2_deployed_runtime import ACCOUNT, IMAGE, SLUG, Pods, _cs, _environ, _store
from tests.test_worker_home import inprocess as inprocess

pytestmark = pytest.mark.unit


@pytest.fixture
def world(tmp_path, monkeypatch):
    store = _store()
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient.join", lambda *args: None)
    pods = Pods("swarm-pod-proof")
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    monkeypatch.setattr(deployed.image_tag, "resolve", lambda repository, tag: IMAGE)
    seats = MasterSeats(store.redis)
    seats.set_count(SLUG, 2)
    local = FakeRuntime()

    def start(seat=None):
        if seat:
            seats.own_hive(SLUG, "anton", f"{seat}@{SLUG}")
        service = _cs().host(_environ(tmp_path, store), store, "anton")
        runtime = service.runtime
        runtime.herdr_runtime = local
        runtime.router.runtimes[LOCAL] = LocalHerdrRuntime(local)
        runtime.launch.func.__self__.planned = lambda task: None
        return service, runtime

    yield store, pods, local, start


@pytest.mark.parametrize("seat", ["master", "master-2"])
def test_configured_master_seat_runs_as_a_native_master_pod(world, seat):
    from scripts.swarm import master_launch

    store, pods, local, start = world
    service, inner = start(seat)
    runtime = controller.FencedRuntime(store, SLUG, service.controller.held, inner, "distributed")
    try:
        if seat == "master":
            launched = master_launch.up(store, SLUG, runtime, 100, "new", None, None)
            assert launched.seat == f"master@{SLUG}"
        else:
            lead = store.next_name(SLUG, "master")
            local.live.add(lead)
            store.put_agent(SLUG, AgentRecord(lead, "master", "master", seat=f"master@{SLUG}"))
            actions = master_fill.run(SLUG, store.config(SLUG), store, runtime, {}, 100)
            assert len(actions) == 1 and "spawned" in actions[0]
        assert local.masters == []
        [pod] = pods.created
        execution_id = pod["metadata"]["labels"][spec.EXECUTION_LABEL]
        execution = store.execution(SLUG, execution_id)
        assert (execution.lane, execution.task, execution.seat, execution.profile) == (
            "master",
            seat,
            f"{seat}@{SLUG}",
            "master",
        )
        assert (execution.runtime_backend, execution.account, execution.generation) == ("kubernetes", ACCOUNT, 1)
        launch = json.loads(pods.maps[0]["data"]["launch.json"])
        assert launch["profile"] == "master"
        assert launch["authority"]["task_id"] == seat
        assert launch["authority"]["seat_id"] == f"{seat}@{SLUG}"
        assert "master" in launch["prompt"]
        assert pod["metadata"]["annotations"][spec.DOMAIN + "/profile"] == "general"
    finally:
        service.stop()


@pytest.mark.parametrize("seat", ["master", "master-2"])
def test_unconfigured_master_seats_keep_local_placement(world, seat):
    store, pods, local, start = world
    service, runtime = start()
    try:
        task = {"id": seat, "seat": f"{seat}@{SLUG}"}
        placed = runtime.spawn(store.config(SLUG), "master", store.next_name(SLUG, "master"), task)
        assert placed.placement == ""
        assert len(local.masters) == 1
        assert pods.created == []
    finally:
        service.stop()


def test_a_master_owning_another_hive_stays_off_this_controllers_pods(world):
    store, pods, local, start = world
    MasterSeats(store.redis).own_hive(SLUG, "workstation", f"master@{SLUG}")
    service, runtime = start("master-2")
    try:
        assert runtime.router.placed_backend("master", {"id": "master"}) == "local"
        assert runtime.router.placed_backend("master", {"id": "master-2"}) == "kubernetes"
    finally:
        service.stop()


def test_running_controller_resolves_current_master_hive_ownership(world):
    store, pods, local, start = world
    service, runtime = start()
    seats = MasterSeats(store.redis)
    task = {"id": "master-2"}
    try:
        assert runtime.router.placed_backend("master", task) == "local"
        seats.own_hive(SLUG, "anton", f"master-2@{SLUG}")
        assert runtime.router.placed_backend("master", task) == "kubernetes"
        seats.own_hive(SLUG, "anton", f"master@{SLUG}")
        assert runtime.router.placed_backend("master", task) == "local"
        assert runtime.router.placed_backend("master", {"id": "master"}) == "kubernetes"
        seats.own_hive(SLUG, "anton", f"master-2@{SLUG}")
        seats.set_count(SLUG, 1)
        assert runtime.router.placed_backend("master", task) == "local"
    finally:
        service.stop()


def test_a_local_extra_master_seat_does_not_prevent_a_later_anton_seat(world):
    store, pods, local, start = world
    MasterSeats(store.redis).set_count(SLUG, 3)
    service, inner = start("master-3")
    runtime = controller.FencedRuntime(store, SLUG, service.controller.held, inner, "distributed")
    lead = store.next_name(SLUG, "master")
    local.live.add(lead)
    store.put_agent(SLUG, AgentRecord(lead, "master", "master", seat=f"master@{SLUG}"))
    try:
        actions = master_fill.run(SLUG, store.config(SLUG), store, runtime, {}, 100)
        assert len(actions) == 2
        assert actions[0] == "the workstation hive spawns master work"
        assert "spawned" in actions[1]
        assert local.masters == []
        [pod] = pods.created
        execution = store.execution(SLUG, pod["metadata"]["labels"][spec.EXECUTION_LABEL])
        assert (execution.lane, execution.task, execution.profile) == ("master", "master-3", "master")
    finally:
        service.stop()


def test_the_shipped_master_profile_materializes_as_a_master_worker_home(tmp_path):
    root = tmp_path / "attempts"
    root.mkdir(mode=0o700)
    record = worker_home.bootstrap(
        worker_home.Request(
            root,
            "master-proof",
            PACKAGE_ROLES,
            {"claude": "master"},
            Path(sys.executable),
            {},
            {},
            os.getuid(),
            os.getgid(),
        )
    )
    assert record["profiles"] == {"claude": "master"}
    home = root / "master-proof" / record["homes"]["claude"]
    persona = (home / ".claude" / "CLAUDE.md").read_text()
    assert "Swarm Master" in persona
    assert "never edit code or config files" in persona
    assert "claim a task: work that needs a repository change goes to a lane as a task" in persona
    assert "Role: Swarm Engineer" not in persona
