import pytest

from scripts.hive.placement import NO_UI_HIVE, Choice, Wait, choose, place
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive
from tests.hive.test_placement import NOW, REPO, hive

OFF_HIVE_LANE = "unhived"


def two_hosts(setup, anton_agents=0):
    from scripts.hive import registry

    setup.store.redis.delete(registry.key("remote"))
    setup.store.redis.srem(registry.INDEX, "remote")
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=10)
    write_hive(setup, id="anton", ui="no", excluded=["master", "qa", "frontend"], max_agents=2)
    setup.store.redis.set(f"{registry.key('anton')}:sessions", f'{{"eng": {anton_agents}}}')


ROLES = {
    "frontend engineer": ("eng", "frontend", {"id": "task"}, "workstation"),
    "qa agent": ("eng", "qa", {"id": "task"}, "workstation"),
    "master": ("master", "master", {"id": "master"}, "workstation"),
    "engineer": ("eng", "engineer", {"id": "task"}, "anton"),
    "planner": ("plan", "planner", {"id": "task"}, "anton"),
    "dispatcher": ("dispatch", "dispatcher", {"id": "dispatcher"}, "anton"),
}


@pytest.mark.parametrize(("lane", "profile", "task", "host"), ROLES.values(), ids=ROLES.keys())
def test_each_role_lands_on_its_host_kind(setup, lane, profile, task, host):
    from scripts.hive.launch import Selection
    from scripts.swarm.profile_choice import ProfileDecision

    two_hosts(setup)
    setup.herdr._profile_decision.return_value = ProfileDecision(profile, "task", profile)
    selected = setup.launcher.select(setup.config, lane, task)
    assert isinstance(selected, Selection)
    assert selected.hive == host
    assert selected.fallback == ""


def test_a_full_headless_fleet_falls_back_to_the_workstation_with_the_reason_recorded(setup):
    from scripts.swarm_v2.runtime.base import SpawnRequest

    two_hosts(setup, anton_agents=2)
    task = {"id": "task", "seat": "eng-1@fixture", "controller_epoch": setup.held.epoch}
    selected = setup.launcher.select(setup.config, "eng", task)
    assert selected.hive == "workstation"
    assert selected.fallback == "no headless hive for eng has room"
    name = setup.store.next_name(setup.config.slug, "eng")
    admitted = setup.launcher.admit(SpawnRequest(setup.config, "eng", name, task), selected)
    assert admitted.profile_decision["placement_fallback"] == "no headless hive for eng has room"


def test_a_task_pinned_to_a_ui_hive_records_the_pin_as_its_reason(setup):
    two_hosts(setup)
    selected = setup.launcher.select(setup.config, "eng", {"id": "task", "hive": "workstation"})
    assert (selected.hive, selected.fallback) == ("workstation", "the task is pinned to workstation")
    selected = setup.launcher.select(setup.config, "eng", {"id": "task", "hive": "anton"})
    assert (selected.hive, selected.fallback) == ("anton", "")


def test_an_account_a_headless_hive_advertises_wins_over_an_earlier_account_that_only_fits_the_workstation(setup):
    from scripts.hive import registry
    from scripts.swarm.capacity import Account

    two_hosts(setup)
    write_hive(
        setup,
        id="workstation",
        ui="yes",
        excluded=[],
        max_agents=10,
        slots=[
            {"harness": "claude", "account": "first", "kind": "subscription"},
        ],
    )
    write_hive(
        setup,
        id="anton",
        ui="no",
        excluded=["master", "qa", "frontend", "dispatch"],
        max_agents=2,
        slots=[
            {"harness": "claude", "account": "second", "kind": "subscription"},
        ],
    )
    setup.herdr._quota_accounts = [
        Account("claude", "first", "room", 0, 80, 80, 6),
        Account("claude", "second", "room", 1, 80, 80, 6),
    ]
    selected = setup.launcher.select(setup.config, "eng", {"id": "task"})
    assert (selected.hive, selected.account.name, selected.fallback) == ("anton", "second", "")
    setup.store.redis.set(f"{registry.key('anton')}:sessions", '{"eng": 2}')
    selected = setup.launcher.select(setup.config, "eng", {"id": "task"})
    assert (selected.hive, selected.account.name) == ("workstation", "first")
    assert selected.fallback == "no headless hive for eng has room"


def test_the_hive_controller_routes_the_plan_lane_through_hive_placement(setup):
    from scripts.swarm import controller
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    two_hosts(setup)
    setup.herdr._profile_decision.return_value = ProfileDecision("planner", "lane", "planner")
    routed = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    fenced = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, routed, "compose")
    assert fenced.runtime.router.placed_backend("plan", {"id": "task"}) == "hive"
    assert fenced.placement_refusal("plan", {"id": "task"}) == ""
    assert fenced.runtime.hive_launch.select(setup.config, "plan", {"id": "task"}).hive == "anton"


MASTER_WAIT = "no live hive can run the master with interactive, subscription or api authentication"


@pytest.mark.parametrize("lacks", [{"repos": []}, {"slots": []}], ids=["repo", "slot"])
def test_a_master_with_no_fitting_ui_hive_is_left_to_the_workstation_when_headless_hives_exclude_it(setup, lacks):
    from scripts.hive.placement import Wait
    from scripts.swarm import controller
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    two_hosts(setup)
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=10, **lacks)
    write_hive(setup, id="anton", ui="no", excluded=["master"], max_agents=2)
    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    assert setup.launcher.select(setup.config, "master", {"id": "master"}) == Wait(MASTER_WAIT)
    routed = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    fenced = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, routed, "compose")
    refused = fenced.placement_refusal("master", {"id": "master"})
    assert refused == f"the workstation hive spawns master work: {MASTER_WAIT}"
    with pytest.raises(controller.SwarmError, match="the workstation hive spawns master work"):
        fenced.spawn(setup.config, "master", "master name", {"id": "master"})


def test_a_dispatcher_spawn_lands_on_a_headless_hive_and_takes_the_workstation_only_when_none_has_room(setup):
    from scripts.hive import registry
    from scripts.swarm import controller
    from scripts.swarm.dispatch_seat import SEAT
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.runtime.base import RuntimeRouter, SpawnRequest
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    two_hosts(setup)
    setup.herdr._profile_decision.return_value = ProfileDecision("dispatcher", "lane", "dispatcher")
    routed = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    fenced = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, routed, "compose")
    task = {"id": SEAT, "seat": "dispatcher@fixture", "controller_epoch": setup.held.epoch}
    assert fenced.runtime.router.placed_backend("dispatch", task) == "hive"
    assert fenced.placement_refusal("dispatch", {"id": SEAT}) == ""
    selected = fenced.runtime.hive_launch.select(setup.config, "dispatch", task)
    assert (selected.hive, selected.fallback) == ("anton", "")
    setup.store.redis.set(f"{registry.key('anton')}:sessions", '{"dispatch": 2}')
    selected = fenced.runtime.hive_launch.select(setup.config, "dispatch", task)
    assert (selected.hive, selected.fallback) == ("workstation", "no headless hive for dispatch has room")
    name = setup.store.next_name(setup.config.slug, "dispatch")
    admitted = setup.launcher.admit(SpawnRequest(setup.config, "dispatch", name, task), selected)
    assert admitted.profile_decision["placement_fallback"] == "no headless hive for dispatch has room"


def hive_controller(setup):
    from scripts.swarm import controller
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    routed = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    return controller.FencedRuntime(setup.store, setup.config.slug, setup.held, routed, "compose")


@pytest.mark.parametrize("seat", ["master", "master-2", "master-3"])
def test_every_master_seat_is_placed_by_the_ui_host_rule(setup, monkeypatch, seat):
    from scripts.swarm import controller
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.masters import MasterSeats

    monkeypatch.setattr(controller, "_workstation_herdr", lambda: False)
    MasterSeats(setup.store.redis).set_count(setup.config.slug, 3)
    two_hosts(setup)
    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    fenced = hive_controller(setup)
    assert fenced.placement_refusal("master", {"id": seat}) == ""
    assert fenced.runtime.router.placed_backend("master", {"id": seat}) == "hive"
    assert fenced.runtime.hive_launch.select(setup.config, "master", {"id": seat}).hive == "workstation"
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=10, repos=[])
    write_hive(setup, id="anton", ui="no", excluded=["master"], max_agents=2)
    assert fenced.placement_refusal("master", {"id": seat}) == f"the workstation hive spawns master work: {MASTER_WAIT}"


@pytest.mark.parametrize("seat", ["master", "master-2"])
def test_a_refused_master_spawns_on_the_workstation_herdr_server(setup, monkeypatch, seat):
    from unittest.mock import Mock

    from scripts.swarm import controller
    from scripts.swarm.profile_choice import ProfileDecision

    two_hosts(setup)
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=10, repos=[])
    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    fenced = hive_controller(setup)
    fenced.runtime.hive_launch.spawn = Mock()
    setup.herdr.spawn.return_value = "placed on the workstation"
    monkeypatch.setattr(controller, "_workstation_herdr", lambda: True)
    assert fenced.placement_refusal("master", {"id": seat}) == ""
    assert fenced.spawn(setup.config, "master", "master name", {"id": seat}) == "placed on the workstation"
    _, lane, name, task = setup.herdr.spawn.call_args.args
    assert (lane, name, task["id"], task["controller_epoch"]) == ("master", "master name", seat, setup.held.epoch)
    fenced.spawn(setup.config, "master", "master name", {})
    assert setup.herdr.spawn.call_args.args[3]["id"] == "master"
    setup.herdr.spawn.reset_mock()
    monkeypatch.setattr(controller, "_workstation_herdr", lambda: False)
    with pytest.raises(controller.SwarmError, match=f"the workstation hive spawns master work: {MASTER_WAIT}"):
        fenced.spawn(setup.config, "master", "master name", {"id": seat})
    setup.herdr.spawn.assert_not_called()
    fenced.runtime.hive_launch.spawn.assert_not_called()


def test_only_master_work_moves_to_the_workstation_herdr_server(setup, monkeypatch):
    from scripts.swarm import controller

    two_hosts(setup)
    fenced = hive_controller(setup)
    monkeypatch.setattr(controller, "_workstation_herdr", lambda: True)
    assert (
        fenced.placement_refusal(OFF_HIVE_LANE, {"id": OFF_HIVE_LANE})
        == f"the workstation hive spawns {OFF_HIVE_LANE} work"
    )
    with pytest.raises(controller.SwarmError, match=f"the workstation hive spawns {OFF_HIVE_LANE} work"):
        fenced.spawn(setup.config, OFF_HIVE_LANE, f"{OFF_HIVE_LANE} name", {"id": OFF_HIVE_LANE})
    setup.herdr.spawn.assert_not_called()


def test_a_refused_hive_lane_stays_refused_when_the_workstation_herdr_answers(setup, monkeypatch):
    from scripts.swarm import controller
    from scripts.swarm.profile_choice import ProfileDecision

    two_hosts(setup)
    setup.herdr._profile_decision.return_value = ProfileDecision("qa", "task", "qa")
    fenced = hive_controller(setup)
    monkeypatch.setattr(controller, "_workstation_herdr", lambda: True)
    task = {"id": "task", "hive": "anton"}
    assert fenced.placement_refusal("eng", task) == "no live hive takes qa"
    with pytest.raises(controller.SwarmError, match="no live hive takes qa"):
        fenced.spawn(setup.config, "eng", "eng name", task)
    setup.herdr.spawn.assert_not_called()


def test_master_fill_spawns_a_refused_seat_two_master_on_the_workstation_herdr_server(monkeypatch):
    import fakeredis

    from scripts.swarm import controller, lease
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm_v2 import master_fill
    from scripts.swarm_v2.masters import MasterSeats
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
    from scripts.swarm_v2.runtime.routed import RoutedRuntime
    from tests.swarm.test_tick import FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    MasterSeats(store.redis).set_count("sw", 2)
    herdr = FakeRuntime()
    herdr._profile_decision = lambda *args: ProfileDecision("master", "lane", "master")
    herdr._quota_accounts = []
    held = lease.acquire(store, "sw", "controller")
    routed = RoutedRuntime(herdr, RuntimeRouter([LocalHerdrRuntime(herdr)]))
    fenced = controller.FencedRuntime(store, "sw", held, routed, "compose")
    monkeypatch.setattr(controller, "_workstation_herdr", lambda: False)
    actions = master_fill.run("sw", store.config("sw"), store, fenced, {}, 1)
    assert actions == [f"the workstation hive spawns master work: {NO_UI_HIVE}"]
    assert herdr.masters == []
    monkeypatch.setattr(controller, "_workstation_herdr", lambda: True)
    actions = master_fill.run("sw", store.config("sw"), store, fenced, {}, 2)
    assert len(actions) == 1 and actions[0].startswith("spawned master ")
    [(_, task)] = herdr.masters
    assert (task["id"], task["controller_epoch"]) == ("master-2", held.epoch)
    assert actions[0].endswith("in seat master-2@sw")


def test_only_the_counted_master_seats_route_to_hive_placement(setup):
    from scripts.swarm_v2.masters import MasterSeats

    MasterSeats(setup.store.redis).set_count(setup.config.slug, 3)
    router = hive_controller(setup).runtime.router
    assert [router.placed_backend("master", {"id": seat}) for seat in ("master-0", "master-3", "master-4")] == [
        "local",
        "hive",
        "local",
    ]


def test_the_workstation_probe_asks_the_herdr_server_named_by_the_environment(monkeypatch):
    import os

    from scripts import herdr_host
    from scripts.swarm import controller

    seen = []
    monkeypatch.setattr(herdr_host, "server_running", lambda environ: seen.append(environ) or True)
    assert controller._workstation_herdr() is True
    assert seen == [dict(os.environ)]


@pytest.mark.parametrize(
    ("task", "placed"),
    [({"id": "d"}, "original runtime"), ({"id": "d", "hive": "workstation"}, "hive runtime")],
    ids=["unpinned", "pinned"],
)
def test_a_non_hive_lane_spawns_through_the_original_runtime_unless_pinned_to_a_hive(setup, task, placed):
    from unittest.mock import Mock

    fenced = hive_controller(setup)
    fenced.runtime.hive_launch.select = Mock(return_value=Choice("workstation"))
    original = Mock()
    original.router.placed_backend.return_value = "kubernetes"
    original.router.enabled_backend.return_value = "kubernetes"
    original.spawn.return_value = "original runtime"
    fenced.runtime.original_runtime = original
    fenced.runtime.spawn = Mock(return_value="hive runtime")
    assert fenced.placement_refusal(OFF_HIVE_LANE, task) == ""
    assert fenced.spawn(setup.config, OFF_HIVE_LANE, f"{OFF_HIVE_LANE} name", task) == placed


def test_a_runtime_without_hive_launch_spawns_a_non_hive_lane_through_itself(setup):
    from scripts.swarm import controller
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
    from scripts.swarm_v2.runtime.routed import RoutedRuntime
    from tests.swarm.test_tick import FakeRuntime

    herdr = FakeRuntime()
    routed = RoutedRuntime(herdr, RuntimeRouter([LocalHerdrRuntime(herdr)]))
    fenced = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, routed, controller.LOCAL)
    fenced.spawn(setup.config, OFF_HIVE_LANE, f"{OFF_HIVE_LANE} name", {"id": "d"})
    assert herdr.spawned == [(OFF_HIVE_LANE, f"{OFF_HIVE_LANE} name", "d")]


def host(hive_id: str, ui: str, **fields) -> dict:
    excluded = ["master", "qa", "frontend"] if ui == "no" else []
    return hive(hive_id, **{"ui": ui, "excluded": excluded, "max_agents": 4, **fields})


FALLBACKS = {
    "headless with room": ([host("ws", "yes"), host("anton", "no", agents=3)], Choice("anton"), "subscription"),
    "headless full": (
        [host("ws", "yes"), host("anton", "no", agents=4)],
        Choice("ws", "no headless hive for eng has room"),
        "subscription",
    ),
    "no headless live": (
        [host("ws", "yes"), host("anton", "no", heartbeat_at=0)],
        Choice("ws", "no live headless hive takes eng"),
        "subscription",
    ),
    "headless without the repo falls back": (
        [host("ws", "yes"), host("anton", "no", repos=["other"])],
        Choice("ws", "no headless hive for eng lists the repo agentihooks"),
        "subscription",
    ),
    "headless with one free place but without the slot falls back": (
        [host("ws", "yes"), host("anton", "no", agents=3, slots=[])],
        Choice("ws", "no headless hive for eng advertises the claude slot acct"),
        "subscription",
    ),
    "the fallback honours prefer among UI hives": (
        [
            host("ws", "yes"),
            host("vm", "yes", agents=3, prefer={"eng": 1}),
            host("anton", "no", agents=4),
        ],
        Choice("vm", "no headless hive for eng has room"),
        "subscription",
    ),
    "the fallback checks the login on the UI hive": (
        [host("ws", "yes", interactive={"claude": ""}), host("anton", "no", agents=4)],
        Wait("no headless hive for eng has room"),
        "interactive",
    ),
    "headless without the login falls back": (
        [host("ws", "yes"), host("anton", "no", interactive={"claude": ""})],
        Choice("ws", "no headless hive for eng holds the claude login for acct"),
        "interactive",
    ),
    "one full and one without the slot falls back": (
        [host("ws", "yes"), host("anton", "no", agents=4), host("pod", "no", slots=[])],
        Choice("ws", "no headless hive for eng advertises the claude slot acct"),
        "subscription",
    ),
    "full headless and a full workstation wait": (
        [host("ws", "yes", agents=4), host("anton", "no", agents=4)],
        Wait("no headless hive for eng has room"),
        "subscription",
    ),
    "only headless": ([host("anton", "no", agents=1), host("pod", "no", agents=2)], Choice("anton"), "subscription"),
}


@pytest.mark.parametrize("case", FALLBACKS.values(), ids=FALLBACKS.keys())
def test_a_non_ui_role_takes_a_ui_hive_only_when_no_headless_hive_takes_it(case):
    hives, expected, route = case
    assert choose("eng", "claude", "acct", route, REPO, hives, NOW) == expected


def test_a_fleet_without_a_ui_hive_keeps_the_plain_reason():
    assert place("eng", "claude", "acct", "subscription", REPO, [host("anton", "no", agents=4)], NOW) == Wait(
        "no hive for eng has room"
    )


def test_ui_roles_prefer_ui_hives_and_fall_back_to_a_headless_hive_that_allows_them():
    hives = [host("ws", "yes", agents=3), host("anton", "no", excluded=[])]
    for role in ("qa", "frontend", "master"):
        assert choose(role, "claude", "acct", "subscription", REPO, hives, NOW) == Choice("ws")
    beside_a_master_pc = [host("pc", "yes", excluded=["qa", "frontend", "eng", "ci", "plan", "dispatch"]), hives[1]]
    assert choose("qa", "claude", "acct", "subscription", REPO, beside_a_master_pc, NOW) == Choice(
        "anton", "no live UI hive takes qa"
    )
