from unittest.mock import Mock

import pytest

from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive


def master(setup):
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    write_hive(setup, roles=["master"], ui="yes")
    return {"id": "master"}


def test_master_selects_an_interactive_hive_without_controller_credentials(setup):
    from scripts.hive.launch import Selection
    from scripts.swarm.capacity import Account

    task = master(setup)
    setup.herdr._quota_accounts = []
    write_hive(setup, id="login", roles=["master"], ui="yes", interactive={"claude": "native"}, slots=[])
    selected = setup.launcher.select(setup.config, "master", task)
    assert isinstance(selected, Selection)
    assert selected.account == Account("claude", "native", "room", 0, None, None, 1, kind="interactive")
    assert (selected.hive, selected.account.name, selected.route_kind) == ("login", "native", "interactive")
    setup.herdr.choose.assert_not_called()


def test_successor_replaces_saved_fallback_auth_with_the_best_login(setup):
    from scripts.swarm import tick

    master(setup)
    envelope = {
        "launch": {"profile": "master", "harness": "claude", "account": "account", "model": "opus", "effort": "high"}
    }
    setup.store.put_handoff(setup.config.slug, "master", "continue the master", "master@fixture", envelope)
    task = tick.primed(setup.store, setup.config.slug, "master@fixture", {"id": "master"})
    assert task["handoff_envelope"] == envelope
    assert "route_kind" not in task
    assert "hive" not in task
    assert setup.launcher.select(setup.config, "master", task).route_kind == "subscription"
    write_hive(setup, id="login", roles=["master"], ui="yes", interactive={"claude": "native"}, slots=[])
    selected = setup.launcher.select(setup.config, "master", task)
    assert (selected.hive, selected.account.name, selected.route_kind) == ("login", "native", "interactive")


def test_master_admission_records_fallback_auth_and_ui_placement(setup):
    from scripts.swarm.tick import MASTER
    from scripts.swarm_v2.runtime.base import SpawnRequest

    task = master(setup)
    name = setup.store.next_name(setup.config.slug, MASTER)
    task.update(seat="master@fixture", controller_epoch=setup.held.epoch)
    request = SpawnRequest(setup.config, MASTER, name, task)
    selected = setup.launcher.select(setup.config, MASTER, task)
    admitted = setup.launcher.admit(request, selected)
    assert admitted.desired_route_kind == "interactive"
    assert admitted.profile_decision["master_placement"] == {"hive": "remote", "headless": False}
    stored = setup.store.execution_occupants(setup.config.slug)["master@fixture"]
    assert stored.desired_route_kind == "interactive"
    assert stored.profile_decision["master_placement"] == admitted.profile_decision["master_placement"]


def test_controller_routes_an_unpinned_primary_master_through_hive_placement(setup):
    from scripts.swarm import controller
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    task = master(setup)
    fenced = controller.FencedRuntime(
        setup.store, setup.config.slug, setup.held, RoutedRuntime(setup.herdr, RuntimeRouter([])), "compose"
    )
    assert fenced.placement_refusal("master", {}) == ""
    assert fenced.placement_refusal("master", task) == ""
    assert fenced.runtime.router.placed_backend("master", task) == "hive"
    fenced.runtime.spawn = Mock(return_value="hive master")
    assert fenced.spawn(setup.config, "master", "name", task) == "hive master"
    fenced.runtime.spawn.assert_called_once_with(
        setup.config, "master", "name", {**task, "controller_epoch": setup.held.epoch}
    )


@pytest.mark.parametrize("route", ["interactive", "subscription", "api"])
def test_master_order_preserves_placement_health_through_the_consumer(setup, route):
    from types import SimpleNamespace

    from scripts.hive import launch, orders
    from scripts.swarm import tick
    from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status

    task = master(setup)
    if route == "interactive":
        write_hive(setup, roles=["master"], ui="yes", interactive={"claude": "account"})
    elif route == "api":
        write_hive(
            setup, roles=["master"], ui="yes", slots=[{"harness": "claude", "account": "account", "kind": "api"}]
        )
    name = setup.store.next_name(setup.config.slug, "master")
    task.update(seat="master@fixture", controller_epoch=setup.held.epoch)
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        tick.Placed("pane", "claude", "account", profile="master", profile_decision={"validation": {"pid": 12}}),
    )
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    consumer = orders.Consumer(setup.store.redis, "remote", runtime)

    def consume(_):
        assert consumer.poll() == 1

    setup.launcher.sleep = consume
    assert isinstance(setup.launcher, launch.HiveLaunch)
    outcome = setup.launcher.spawn(SpawnRequest(setup.config, "master", name, task))
    assert outcome.ok
    assert outcome.value.route_kind == route
    assert outcome.value.desired_route_kind == "interactive"
    assert outcome.value.profile_decision["master_placement"] == {"hive": "remote", "headless": False}
    assert outcome.value.execution.profile_decision == outcome.value.profile_decision
    assert outcome.value.execution.desired_route_kind == "interactive"


@pytest.mark.parametrize("lane", ["master", "eng"])
@pytest.mark.parametrize("route", ["subscription", "api", "interactive"])
def test_a_master_fallback_order_hands_its_route_to_the_runtime_unchanged(setup, lane, route, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from hooks import config as hooks_config
    from scripts.hive import orders
    from scripts.swarm import lease, tick
    from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status

    task = master(setup)
    monkeypatch.setattr(hooks_config, "MASTER_CLAUDE_LOGIN", True)
    write_hive(setup, roles=["master"], ui="yes", interactive={"claude": "account"})
    name = setup.store.next_name(setup.config.slug, lane)
    task.update(seat=f"{lane}@fixture", controller_epoch=setup.held.epoch, route_kind=route)
    request = SpawnRequest(setup.store.config(setup.config.slug), lane, name, task)
    selected = setup.launcher.select(request.config, lane, task)
    admitted = setup.launcher.admit(request, selected)
    assert admitted.desired_route_kind == ""
    order = orders.LaunchOrder(
        setup.config.slug,
        admitted.execution_id,
        admitted.generation,
        "master",
        lane,
        "master",
        "account",
        "claude",
        route,
        "prompt",
        setup.held.epoch,
        lease.now_ms(setup.store) + 60000,
    )
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        tick.Placed("pane", "claude", "account", profile="master", profile_decision={"validation": {"pid": 12}}),
    )
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    consumer = orders.Consumer(setup.store.redis, "remote", runtime)
    assert consumer.launch(order, admitted, task).status == "live"
    sent = runtime.spawn.call_args.args[0]
    assert sent.config == replace(request.config, lanes={lane: {"agent": "claude", "role": "master"}})
    assert sent.task["route_kind"] == route
    assert sent.task["launch_assignment"]["account"] == "account"


@pytest.mark.parametrize("route", ["subscription", "api"])
def test_an_explicit_master_route_keeps_its_account_when_a_native_login_is_available(setup, route):
    task = master(setup)
    write_hive(setup, id="login", roles=["master"], ui="yes", interactive={"claude": "native"}, slots=[])
    selected = setup.launcher.select(setup.config, "master", {**task, "route_kind": route})
    assert (selected.hive, selected.account.name, selected.route_kind) == ("remote", "account", route)
    assert selected.master_placement is None


@pytest.mark.parametrize("harness,state,sessions", [("claude", "full", 6), ("codex", "room", 0)])
def test_master_refuses_unavailable_or_foreign_accounts_and_records_its_wait_lane(setup, harness, state, sessions):
    from scripts.hive import placement, placement_waits
    from scripts.swarm.capacity import Account

    task = master(setup)
    setup.herdr._quota_accounts = [Account(harness, "account", state, sessions, 80, 80, 6)]
    expected = placement.Wait("no live hive can run the master with interactive, subscription or api authentication")
    assert setup.launcher.select(setup.config, "master", task) == expected
    found = placement_waits.read(setup.store, setup.config.slug)["master"]
    assert found["lane"] == "master"
    assert found["reason"] == expected.reason


def test_a_pinned_secondary_master_uses_hive_selection_before_its_original_router(setup):
    from scripts.swarm import controller
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    master(setup)
    fenced = controller.FencedRuntime(
        setup.store, setup.config.slug, setup.held, RoutedRuntime(setup.herdr, RuntimeRouter([])), "compose"
    )
    assert fenced.placement_refusal("master", {"id": "master-2", "hive": "remote"}) == ""


def test_master_matches_a_dashed_controller_key_to_the_hive_slot_it_names(setup):
    from scripts.swarm.capacity import Account

    task = master(setup)
    setup.herdr._quota_accounts = [Account("claude", "user-atmail-com", "OPEN", 0, 80, 80, 6)]
    write_hive(
        setup,
        roles=["master"],
        ui="yes",
        slots=[{"harness": "claude", "account": "user_atmail_com", "kind": "subscription", "cap": 6}],
    )
    selected = setup.launcher.select(setup.config, "master", task)
    assert (selected.hive, selected.route_kind) == ("remote", "subscription")
    assert selected.account == Account("claude", "user_atmail_com", "OPEN", 0, 80, 80, 6)


@pytest.mark.parametrize(("slot", "cap"), [({"cap": 6}, 6), ({"cap": 0}, 0), ({}, 1)])
def test_master_on_a_hive_reserves_against_the_cap_that_hive_reports(setup, slot, cap):
    from scripts.swarm.capacity import Account

    task = master(setup)
    setup.herdr._quota_accounts = [Account("claude", "user-atmail-com", "OPEN", 0, None, None, 1)]
    write_hive(
        setup,
        roles=["master"],
        ui="yes",
        slots=[{"harness": "claude", "account": "user_atmail_com", "kind": "subscription", **slot}],
    )
    assert setup.launcher.select(setup.config, "master", task).account.cap == cap
