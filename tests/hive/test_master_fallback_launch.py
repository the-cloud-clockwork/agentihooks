from unittest.mock import Mock

import pytest

from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive


def master(setup):
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    write_hive(setup, excluded=[], ui="yes")
    return {"id": "master"}


def test_master_selects_an_interactive_hive_without_controller_credentials(setup):
    from scripts.hive.launch import Selection
    from scripts.swarm.capacity import Account

    task = master(setup)
    setup.herdr._quota_accounts = []
    write_hive(setup, id="login", excluded=[], ui="yes", interactive={"claude": "native"}, slots=[])
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
    write_hive(setup, id="login", excluded=[], ui="yes", interactive={"claude": "native"}, slots=[])
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


def admit_after_starting_orphan(setup, offset, live=False):
    from types import SimpleNamespace

    from scripts.swarm.master_start import DEADLINE_MS
    from scripts.swarm.tick import MASTER
    from scripts.swarm_v2.runtime.base import SpawnRequest
    from tests.swarm.test_execution_retirement import starting_master

    task = master(setup)
    orphan = starting_master(setup.store, setup.config.slug)
    runtime = SimpleNamespace(live_names=lambda: {orphan.name} if live else set(), reported=lambda agent: False)
    setup.store.execution_registry.reconcile(setup.config.slug, runtime, 1 + DEADLINE_MS + offset)
    task.update(seat="master@fixture", controller_epoch=setup.held.epoch)
    request = SpawnRequest(setup.config, MASTER, setup.store.next_name(setup.config.slug, MASTER), task)
    return orphan, lambda: setup.launcher.admit(request, setup.launcher.select(setup.config, MASTER, task))


def test_master_admission_replaces_a_starting_orphan_past_its_startup_window(setup):
    orphan, admit = admit_after_starting_orphan(setup, 0)
    admitted = admit()
    assert (admitted.seat, admitted.generation) == (orphan.seat, orphan.generation + 1)
    assert setup.store.execution_occupants(setup.config.slug)[orphan.seat] == admitted


@pytest.mark.parametrize(("offset", "live"), [(-1, False), (0, True)])
def test_master_admission_is_refused_while_a_starting_orphan_is_in_its_window_or_live(setup, offset, live):
    from scripts.swarm.store import SwarmError

    orphan, admit = admit_after_starting_orphan(setup, offset, live)
    with pytest.raises(SwarmError, match="replacement does not match the current execution"):
        admit()
    assert setup.store.execution_occupants(setup.config.slug)[orphan.seat] == orphan


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


@pytest.mark.parametrize("login", [False, True])
def test_controller_keeps_only_the_workstation_login_master_off_hive_placement(setup, monkeypatch, login):
    from scripts import master_claude_login
    from scripts.swarm import controller
    from scripts.swarm_v2 import masters
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    monkeypatch.setattr(master_claude_login.hooks_config, "MASTER_CLAUDE_LOGIN", login)
    eligible = Mock(wraps=master_claude_login.enabled)
    monkeypatch.setattr(master_claude_login, "enabled", eligible)
    masters.MasterSeats(setup.store.redis).set_count(setup.config.slug, 3)
    task = master(setup)
    assert controller._master_seats(setup.store, setup.config.slug) == (
        frozenset({"master-2", "master-3"}) if login else frozenset({"master", "master-2", "master-3"})
    )
    assert eligible.call_args_list == [
        ((setup.config.slug, f"{seat}@{setup.config.slug}", "master", "claude"),)
        for seat in ("master", "master-2", "master-3")
    ]
    fenced = controller.FencedRuntime(
        setup.store, setup.config.slug, setup.held, RoutedRuntime(setup.herdr, RuntimeRouter([])), "compose"
    )
    assert fenced.runtime.router.placed_backend("master", task) == ("local" if login else "hive")
    assert fenced.runtime.router.placed_backend("master-2", {"id": "master-2"}) == "hive"
    assert fenced.runtime.router.placed_backend("master-3", {"id": "master-3"}) == "hive"
    assert fenced.runtime.router.placed_backend("master-4", {"id": "master-4"}) == "local"


@pytest.mark.parametrize("route", ["interactive", "subscription", "api"])
def test_master_order_preserves_placement_health_through_the_consumer(setup, route):
    from types import SimpleNamespace

    from scripts.hive import launch, orders
    from scripts.swarm import tick
    from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status

    task = master(setup)
    if route == "interactive":
        write_hive(setup, excluded=[], ui="yes", interactive={"claude": "account"})
    elif route == "api":
        write_hive(setup, excluded=[], ui="yes", slots=[{"harness": "claude", "account": "account", "kind": "api"}])
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
    write_hive(setup, excluded=[], ui="yes", interactive={"claude": "account"})
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
    write_hive(setup, id="login", excluded=[], ui="yes", interactive={"claude": "native"}, slots=[])
    selected = setup.launcher.select(setup.config, "master", {**task, "route_kind": route})
    assert (selected.hive, selected.account.name, selected.route_kind) == ("remote", "account", route)
    assert selected.master_placement is None


@pytest.mark.parametrize("harness,state,sessions", [("claude", "full", 6), ("codex", "room", 0)])
def test_master_refuses_unavailable_or_foreign_accounts_and_records_its_wait_lane(setup, harness, state, sessions):
    from scripts.hive import placement, placement_waits
    from scripts.swarm.capacity import Account

    task = master(setup)
    setup.herdr._quota_accounts = [Account(harness, "account", state, sessions, 80, 80, 6)]
    expected = placement.Wait("no hive for master advertises a claude slot")
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
        excluded=[],
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
        excluded=[],
        ui="yes",
        slots=[{"harness": "claude", "account": "user_atmail_com", "kind": "subscription", **slot}],
    )
    assert setup.launcher.select(setup.config, "master", task).account.cap == cap


def master_login_hive(setup):
    from scripts.swarm.capacity import Account

    setup.herdr._quota_accounts = [
        Account("claude", "account", "room", 0, 80, 80, 6),
        Account("claude", "login", "room", 3, 80, 80, 6),
    ]
    write_hive(
        setup,
        excluded=[],
        ui="yes",
        master_login={"claude": "login"},
        slots=[
            {"harness": "claude", "account": "account", "kind": "subscription", "cap": 6},
            {"harness": "claude", "account": "login", "kind": "subscription", "cap": 4},
        ],
    )


@pytest.mark.parametrize(
    ("seat", "expected"),
    [
        ("master@fixture", ("login", "master-login", None)),
        (None, ("login", "master-login", None)),
        ("master-2@fixture", ("account", "subscription", 6)),
    ],
)
def test_the_front_door_master_is_ordered_on_the_hive_master_login_account(setup, seat, expected):
    task = master(setup) if seat is None else {**master(setup), "seat": seat}
    master_login_hive(setup)
    selected = setup.launcher.select(setup.config, "master", task)
    assert (selected.account.name, selected.route_kind, selected.account.cap) == expected


def test_a_master_login_without_a_hive_slot_holds_no_account_cap(setup):
    from scripts.swarm.capacity import Account

    task = {**master(setup), "seat": "master@fixture"}
    master_login_hive(setup)
    write_hive(
        setup,
        excluded=[],
        ui="yes",
        master_login={"claude": "login"},
        slots=[{"harness": "claude", "account": "account", "kind": "subscription", "cap": 6}],
    )
    selected = setup.launcher.select(setup.config, "master", task)
    assert (selected.account.name, selected.route_kind, selected.account.cap) == ("login", "master-login", None)
    assert selected.account == Account("claude", "login", "room", 0, None, None, kind="master-login")


def test_a_secondary_master_on_a_native_login_reserves_one_launch_beside_a_master_login(setup):
    task = {**master(setup), "seat": "master-2@fixture"}
    master_login_hive(setup)
    write_hive(
        setup,
        excluded=[],
        ui="yes",
        interactive={"claude": "login"},
        master_login={"claude": "login"},
        slots=[{"harness": "claude", "account": "login", "kind": "subscription", "cap": 4}],
    )
    selected = setup.launcher.select(setup.config, "master", task)
    assert (selected.account.name, selected.route_kind, selected.account.cap) == ("login", "interactive", 1)


def test_a_front_door_master_on_a_native_hive_reserves_one_launch_while_another_hive_has_a_master_login(setup):
    task = {**master(setup), "seat": "master@fixture"}
    master_login_hive(setup)
    write_hive(setup, excluded=["master"], master_login={"claude": "login"})
    write_hive(
        setup,
        id="desk",
        excluded=[],
        ui="yes",
        interactive={"claude": "native"},
        slots=[{"harness": "claude", "account": "native", "kind": "subscription", "cap": 4}],
    )
    selected = setup.launcher.select(setup.config, "master", task)
    assert (selected.hive, selected.account.name, selected.route_kind, selected.account.cap) == (
        "desk",
        "native",
        "interactive",
        1,
    )


def test_a_front_door_successor_on_a_master_login_hive_starts_on_its_first_order(setup, monkeypatch):
    import json
    from types import SimpleNamespace

    from scripts import master_claude_login
    from scripts.hive import orders
    from scripts.routing import reservation
    from scripts.routing.reservation import LaunchSlots
    from scripts.swarm import tick
    from scripts.swarm_v2.runtime.base import Outcome, SpawnRequest, Status

    monkeypatch.setattr(master_claude_login, "unusable", lambda: "")
    task = master(setup)
    master_login_hive(setup)
    name = setup.store.next_name(setup.config.slug, "master")
    task.update(seat="master@fixture", controller_epoch=setup.held.epoch)
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        tick.Placed("pane", "claude", "login", profile="master", profile_decision={"validation": {"pid": 12}}),
    )
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    consumer = orders.Consumer(setup.store.redis, "remote", runtime)

    def consume(_):
        assert consumer.poll() == 1

    setup.launcher.sleep = consume
    outcome = setup.launcher.spawn(SpawnRequest(setup.config, "master", name, task))
    assert outcome.ok, outcome.detail
    assert (outcome.value.account, outcome.value.route_kind) == ("login", "master-login")
    runtime.terminate.assert_not_called()
    assert LaunchSlots(setup.store.redis).counts() == {}
    raw = setup.store.redis.hget(setup.store.key(setup.config.slug, "controller-launch-orders"), "master")
    order = orders.LaunchOrder(**json.loads(raw))
    assert (order.slot, order.route_kind) == ("login", "master-login")
    assert reservation.master_agents(setup.store, "claude", "login") == 0


def headless_front_door_order(setup, route=""):
    from scripts.hive import orders
    from scripts.swarm import lease
    from scripts.swarm_v2.runtime.base import SpawnRequest

    task = {**master(setup), "seat": "master@fixture", "controller_epoch": setup.held.epoch}
    master_login_hive(setup)
    write_hive(setup, ui="no", excluded=[], master_login={"claude": "login"})
    request = SpawnRequest(setup.config, "master", setup.store.next_name(setup.config.slug, "master"), task)
    admitted = setup.launcher.admit(request, setup.launcher.select(setup.config, "master", task))
    order = orders.LaunchOrder(
        setup.config.slug,
        admitted.execution_id,
        admitted.generation,
        "master",
        "master",
        "master",
        "login",
        "claude",
        route or admitted.route_kind,
        "prompt",
        setup.held.epoch,
        lease.now_ms(setup.store) + 60000,
    )
    return order, admitted, task


@pytest.mark.parametrize("route", ["", "subscription"])
@pytest.mark.parametrize(
    ("enabled", "reason"),
    [
        (False, "the master claude ai login is off on this hive: set AGENTIHOOKS_MASTER_CLAUDE_LOGIN=true"),
        (True, "no master claude ai login: run agentihooks claude login --master"),
    ],
)
def test_a_headless_hive_without_a_usable_master_login_refuses_the_front_door_launch(
    setup, monkeypatch, tmp_path, enabled, reason, route
):
    from hooks import config as hooks_config
    from scripts.hive import orders

    monkeypatch.setattr(hooks_config, "MASTER_CLAUDE_LOGIN", enabled)
    monkeypatch.setattr(hooks_config, "AGENTIHOOKS_HOME", str(tmp_path / "home"))
    order, admitted, task = headless_front_door_order(setup, route)
    runtime = Mock()
    ack = orders.Consumer(setup.store.redis, "remote", runtime).launch(order, admitted, task)
    assert (ack.status, ack.target, ack.failure) == ("failed", {}, reason)
    runtime.spawn.assert_not_called()


def test_a_headless_hive_with_a_usable_master_login_takes_the_front_door_launch(setup, monkeypatch):
    from scripts import master_claude_login
    from scripts.hive import orders
    from scripts.swarm_v2.runtime.base import Outcome, Status

    monkeypatch.setattr(master_claude_login, "unusable", lambda: "")
    order, admitted, task = headless_front_door_order(setup)
    runtime = Mock()
    runtime.spawn.return_value = Outcome("spawn", Status.UNAVAILABLE, "local", detail="spawned")
    ack = orders.Consumer(setup.store.redis, "remote", runtime).launch(order, admitted, task)
    assert ack.failure == "unavailable: spawned"
    runtime.spawn.assert_called_once()


@pytest.mark.parametrize(("route", "failure"), [("subscription", "unavailable: spawned"), ("", "refused")])
def test_a_ui_hive_checks_the_master_login_only_for_a_master_login_order(setup, monkeypatch, route, failure):
    from scripts import master_claude_login
    from scripts.hive import orders
    from scripts.swarm_v2.runtime.base import Outcome, Status

    monkeypatch.setattr(master_claude_login, "unusable", lambda: "refused")
    order, admitted, task = headless_front_door_order(setup, route)
    write_hive(setup, ui="yes")
    runtime = Mock()
    runtime.spawn.return_value = Outcome("spawn", Status.UNAVAILABLE, "local", detail="spawned")
    ack = orders.Consumer(setup.store.redis, "remote", runtime).launch(order, admitted, task)
    assert ack.failure == failure
    assert runtime.spawn.called is (failure != "refused")


def test_a_headless_hive_takes_a_secondary_master_without_the_master_login_check(setup, monkeypatch):
    from dataclasses import replace

    from scripts import master_claude_login
    from scripts.hive import orders
    from scripts.swarm_v2.runtime.base import Outcome, Status

    monkeypatch.setattr(master_claude_login, "unusable", lambda: "refused")
    order, admitted, task = headless_front_door_order(setup, "subscription")
    runtime = Mock()
    runtime.spawn.return_value = Outcome("spawn", Status.UNAVAILABLE, "local", detail="spawned")
    secondary = replace(admitted, seat="master-2@fixture")
    ack = orders.Consumer(setup.store.redis, "remote", runtime).launch(order, secondary, task)
    assert ack.failure == "unavailable: spawned"
