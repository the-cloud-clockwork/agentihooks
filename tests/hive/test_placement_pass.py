import os
from unittest.mock import Mock

import pytest

from scripts.hive.launch import Selection
from scripts.swarm import controller
from scripts.swarm.tick import ACK_LATER, SpawnError, _room_wait, _spawn_order
from scripts.swarm_v2.runtime.base import RuntimeRouter
from scripts.swarm_v2.runtime.routed import RoutedRuntime
from tests.hive.test_controller_launch import request_for, setup, write_hive  # noqa: F401
from tests.swarm.test_tick import FakeLedger


def test_cached_placements_keep_each_task_and_lane_separate(setup):  # noqa: F811
    from dataclasses import replace

    from scripts.hive.placement import Wait

    setup.config = replace(setup.config, lanes={"ci": {"agent": "codex"}})
    original = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    runtime = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, original, "compose")
    launch = runtime.runtime.hive_launch
    launch.hives = Mock(wraps=launch.hives)
    one, two = {"id": "one"}, {"id": "two"}
    ordered(setup, runtime, one)
    first = launch.select(setup.config, "eng", one)
    second = launch.select(setup.config, "eng", two)
    ci = launch.select(setup.config, "ci", one)
    assert first.account.name == second.account.name == "account"
    assert isinstance(ci, Wait)
    assert launch.hives.call_count == 3
    assert launch.select(setup.config, "eng", one) is first
    assert launch.select(setup.config, "eng", two) is second
    assert launch.select(setup.config, "ci", one) is ci
    assert launch.hives.call_count == 3


def ordered(world, runtime, task):
    ledger = FakeLedger([task])
    return _spawn_order(
        world.config.slug, world.config, world.store, [], ledger.rows, ledger.state(world.config.slug), runtime
    )


@pytest.mark.parametrize("refused", [False, True])
def test_ordering_and_spawn_reuse_one_placement_and_the_next_pass_refreshes(setup, refused):  # noqa: F811
    task = {"id": "task", "title": "Launch on a hive"}
    if refused:
        write_hive(setup, excluded=["engineer"])
    original = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    runtime = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, original, "compose")
    launch = runtime.runtime.hive_launch
    launch.opening = setup.launcher.opening
    launch.hives = Mock(wraps=launch.hives)

    queue, waits = ordered(setup, runtime, task)
    assert len(queue) == (0 if refused else 1)
    assert bool(waits) is refused
    assert launch.hives.call_count == 1
    first = launch.select(setup.config, "eng", task)
    if not refused:
        assert _room_wait(runtime, "eng", task) == ""
        request = request_for(setup)
        request.task[ACK_LATER] = True
        with pytest.raises(SpawnError, match="hive launch ordered"):
            runtime.spawn(setup.config, "eng", request.name, request.task)
        order = launch.orders_for(request.task["id"])
        assert order.hive == first.hive
        setup.store.drop_agent(setup.config.slug, request.name)
    assert launch.hives.call_count == 1

    write_hive(setup, excluded=[] if refused else ["engineer"])
    queue, waits = ordered(setup, runtime, task)
    assert len(queue) == (1 if refused else 0)
    assert bool(waits) is not refused
    assert launch.hives.call_count == 2


def test_spawn_pass_ends_the_placement_cache_for_later_launches(setup):  # noqa: F811
    from scripts.swarm.tick import _placement_pass, _spawn

    task = {"id": "task", "title": "Launch on a hive"}
    write_hive(setup, excluded=["engineer"])
    original = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    runtime = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, original, "compose")
    launch = runtime.runtime.hive_launch
    launch.hives = Mock(wraps=launch.hives)
    ledger = FakeLedger([task])
    slug = setup.config.slug

    with _placement_pass(runtime):
        _spawn(slug, setup.config, setup.store, ledger, runtime, ledger.rows, ledger.state(slug), 0)
        assert launch.placement_cache
    assert launch.hives.call_count == 1
    assert launch.placement_cache is None

    write_hive(setup, excluded=[])
    assert launch.select(setup.config, "eng", task).hive
    assert launch.hives.call_count == 2


def test_a_running_tick_ends_the_placement_pass_after_spawning():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import tick
    from tests.swarm.test_tick import FakeRuntime

    class PassRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.passes = []

        def begin_placement_pass(self):
            self.passes.append(("begin", len(self.spawned)))

        def end_placement_pass(self):
            self.passes.append(("end", len(self.spawned)))

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    runtime = PassRuntime()
    tick("sw", store, FakeLedger([{"id": "t1", "lane": "eng"}]), runtime, now_ms=1_000)
    assert runtime.spawned
    assert runtime.passes[-1] == ("end", len(runtime.spawned))


def test_a_placement_pass_loads_the_hives_once_and_the_next_pass_reloads(setup, monkeypatch):  # noqa: F811
    from scripts.hive import launch as launch_module

    loads = []
    loaded = launch_module.loaded_hives
    monkeypatch.setattr(launch_module, "loaded_hives", lambda store: loads.append(store) or loaded(store))
    launch = setup.launcher
    launch.placement_cache = None
    first = launch.hives()
    assert launch.hives() == first
    assert len(loads) == 2
    launch.placement_cache = {}
    passed = launch.hives()
    assert launch.hives() is passed
    assert len(loads) == 3
    launch.placement_cache = {}
    assert launch.hives() is not passed
    assert len(loads) == 4


def test_a_local_slot_limit_leaves_the_pass_hives_unchanged_for_later_tasks(setup, monkeypatch):  # noqa: F811
    import copy

    from scripts.hive import admission
    from scripts.hive.placement import Wait
    from scripts.swarm import commands

    monkeypatch.setattr(commands, "hive_id", lambda: "remote")
    monkeypatch.setattr(admission, "capped", lambda found, agents, maximum: admission.Room(0, "max agents", "full"))
    launch = setup.launcher
    launch.placement_cache = {}
    before = copy.deepcopy(launch.hives())
    assert isinstance(launch.select(setup.config, "eng", {"id": "one"}), Wait)
    assert launch.hives() == before


def test_a_placement_pass_opens_the_master_seat_on_hives_named_for_its_swarm(setup):  # noqa: F811
    write_hive(setup, excluded=["master", "engineer"], master_swarms=[setup.config.slug])
    setup.launcher.placement_cache = {}
    assert [hive["excluded"] for hive in setup.launcher.hives()] == [["engineer"]]


def test_a_placement_pass_reads_the_bundle_revision_once_and_the_next_pass_reads_again(monkeypatch, tmp_path):
    from scripts.swarm import overlays, profile_choice
    from scripts.swarm import runtime as runtime_module
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm.store import SwarmConfig

    reads = iter(("first", "second", "third", "fourth"))
    monkeypatch.setattr(overlays, "revision", lambda: next(reads))
    monkeypatch.setattr(profile_choice, "choose", lambda *args: ProfileDecision("engineer", "task", "engineer"))
    herdr = runtime_module.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("fixture", "agentihooks", 1, 1)

    def revision(task_id):
        return herdr._profile_decision(config, "eng", {}, {"id": task_id}, {}, {}, {}).bundle_revision

    assert herdr.pass_memo is None
    assert [revision("one"), revision("two")] == ["first", "second"]
    herdr.pass_memo = {}
    assert [revision("one"), revision("two")] == ["third", "third"]
    herdr.pass_memo = {}
    assert revision("one") == "fourth"


def test_a_placement_pass_checks_the_workstation_herdr_once_and_the_next_pass_checks_again(
    setup,  # noqa: F811
    monkeypatch,
):
    from scripts import herdr_host

    write_hive(setup, excluded=["engineer"])
    checks = []

    def running(environ):
        checks.append(environ)
        return True

    monkeypatch.setattr(herdr_host, "server_running", running)
    original = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    runtime = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, original, controller.HYBRID)

    runtime.placement_refusal("eng", {"id": "one"})
    runtime.placement_refusal("eng", {"id": "two"})
    assert len(checks) == 2
    runtime.begin_placement_pass()
    assert runtime.runtime.hive_launch.herdr.pass_memo == {}
    runtime.placement_refusal("eng", {"id": "one"})
    runtime.placement_refusal("eng", {"id": "two"})
    assert len(checks) == 3
    runtime.end_placement_pass()
    assert runtime.runtime.hive_launch.herdr.pass_memo is None
    runtime.begin_placement_pass()
    runtime.placement_refusal("eng", {"id": "one"})
    assert len(checks) == 4


def test_a_master_moved_to_the_workstation_shares_the_pass_herdr_check(monkeypatch):
    from scripts import herdr_host

    checks = []

    def running(environ):
        checks.append(environ)
        return True

    monkeypatch.setattr(herdr_host, "server_running", running)
    refused = controller.WORKSTATION.format(lane="master")
    memo = {}
    assert controller._to_workstation("master", refused, memo) is True
    assert controller._to_workstation("master", refused, memo) is True
    assert len(checks) == 1
    assert checks[0] == dict(os.environ)
    assert memo == {"workstation_herdr": True}
    assert controller._to_workstation("master", refused) is True
    assert len(checks) == 2
    assert controller._to_workstation("eng", refused, memo) is False
    assert len(checks) == 2


class _Stop(Exception):
    pass


@pytest.mark.parametrize("step", ["placement_refusal", "spawn"])
def test_master_placement_and_spawn_hand_the_pass_memo_to_the_workstation_check(
    setup,  # noqa: F811
    monkeypatch,
    step,
):
    original = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    runtime = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, original, controller.HYBRID)
    refused = controller.WORKSTATION.format(lane="master")
    monkeypatch.setattr(runtime, "_refusal", lambda lane, task, claim="": refused)
    memos = []

    def to_workstation(lane, seen, memo=None):
        memos.append(memo)
        raise _Stop

    monkeypatch.setattr(controller, "_to_workstation", to_workstation)
    runtime.begin_placement_pass()
    pass_memo = runtime.runtime.hive_launch.herdr.pass_memo
    with pytest.raises(_Stop):
        if step == "spawn":
            runtime._spawn(setup.config, "master", "master@x-1", {"id": "master"})
        else:
            runtime.placement_refusal("master", {"id": "master"})
    assert len(memos) == 1
    assert memos[0] is pass_memo


@pytest.mark.parametrize("in_pass", [False, True])
def test_a_selection_in_one_pass_takes_its_hive_room_from_later_tasks(setup, in_pass):  # noqa: F811
    from dataclasses import replace

    from scripts.hive.placement import Wait
    from tests.hive.test_controller_launch import publish_room

    publish_room(setup.store, "remote", 1)
    launch = setup.launcher
    launch.placement_cache = {} if in_pass else None
    first = launch.select(setup.config, "eng", {"id": "one"})
    second = launch.select(setup.config, "eng", {"id": "two"})
    assert isinstance(first, Selection)
    assert first.hive == "remote"
    if in_pass:
        assert isinstance(second, Wait)
        assert launch.select(setup.config, "eng", {"id": "one"}) is first
        assert [hive["room"] for hive in launch.hives() if hive["id"] == "remote"] == [0]
        master = replace(first, master_placement=Mock())
        assert launch._spend(master) is master
        assert [hive["room"] for hive in launch.hives() if hive["id"] == "remote"] == [0]
    else:
        assert isinstance(second, Selection)


@pytest.mark.parametrize("role", ["eng", "engineer"])
@pytest.mark.parametrize("in_pass", [False, True])
def test_a_refused_hive_reservation_turns_the_placement_into_a_wait(setup, monkeypatch, in_pass, role):  # noqa: F811
    from scripts.hive import admission, launch
    from scripts.hive.placement import Wait
    from scripts.swarm import seat_spawn
    from scripts.swarm_v2.runtime.base import Status
    from tests.hive.test_controller_launch import publish_room

    if role == "engineer":
        monkeypatch.delitem(launch.ROLE_PROFILES, "engineer")
    if in_pass:
        write_hive(setup, id="spare", slots=[])
    publish_room(setup.store, "remote", 2)
    launch = setup.launcher
    launch.placement_cache = {} if in_pass else None
    request = request_for(setup)
    assert isinstance(launch.select(setup.config, "eng", request.task), Selection)
    for member in ("other/engineer-1", "other/engineer-2"):
        assert admission.reserve(setup.store.redis, "remote", member, 2)[0]
    result = launch.spawn(request)
    assert result.status is Status.UNAVAILABLE
    assert "no hive for eng has room: remote" in result.detail
    assert seat_spawn.room_refused(SpawnError(result.detail, "unavailable"))
    waited = launch.select(setup.config, "eng", request.task)
    assert isinstance(waited, Wait)
    assert isinstance(launch.select(setup.config, "eng", {"id": "later"}), Wait)
    if in_pass:
        assert (waited.reason, waited.role) == (result.detail, role)
        rooms = {hive["id"]: hive["room"] for hive in launch.hives()}
        assert rooms["remote"] == 0
        assert rooms["spare"] > 0


@pytest.mark.parametrize(
    "refused",
    [
        "no hive for eng has room: remote cpu: five minute load 1.17 on 8 CPUs, fits 0 more",
        "no hive has room, and the local machine runs 4 of its 4 agents",
        ", and the local machine runs 4 of its 4 agents: cpu: five minute load 1.17 on 8 CPUs",
    ],
)
def test_a_refused_hive_reservation_waits_when_placement_no_longer_reports_it(refused):
    from types import SimpleNamespace

    import fakeredis

    from scripts.handoff import transfers
    from scripts.swarm.store import MASTER, RedisStore, SwarmConfig
    from scripts.swarm.tick import tick
    from tests.swarm.test_tick import FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}])
    transfers.record(store, "sw", SimpleNamespace(name="old", seat="master@sw", task=MASTER), "recycle", "", 500)
    actions = tick("sw", store, ledger, FakeRuntime(crash=SpawnError(refused, "unavailable")), now_ms=1_000)
    assert not [action for action in actions if "spawn failed" in action]
    assert f"task t1 waits: {refused}" in actions
    assert f"the master waits: {refused}" in actions
    assert store.redis.get(store.key("sw", "master-start")) is None
    [transfer] = transfers.list_transfers(store, "sw")
    assert (transfer["successor"], transfer["binding"]) == ("", {"state": "pending"})
    assert ledger.rows["t1"]["state"] == "open"
    assert [row["state"] for row in store.launches("sw")] == ["waiting"]
