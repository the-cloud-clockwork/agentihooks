from unittest.mock import Mock

import pytest

from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive

pytestmark = pytest.mark.xdist_group("fakeredis")


def master(setup):
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    write_hive(setup, roles=["master"], ui="yes")
    return {"id": "master"}


def test_master_selects_an_interactive_hive_without_controller_credentials(setup):
    from scripts.hive.launch import Selection

    task = master(setup)
    setup.herdr._quota_accounts = []
    write_hive(setup, id="login", roles=["master"], ui="yes", interactive={"claude": "native"}, slots=[])
    selected = setup.launcher.select(setup.config, "master", task)
    assert isinstance(selected, Selection)
    assert (selected.hive, selected.account.name, selected.route_kind) == ("login", "native", "interactive")
    setup.herdr.choose.assert_not_called()


def test_successor_replaces_saved_fallback_auth_with_the_best_login(setup):
    task = master(setup)
    task["launch_assignment"] = {"harness": "claude", "account": "account"}
    assert setup.launcher.select(setup.config, "master", task).route_kind == "subscription"
    write_hive(setup, id="login", roles=["master"], ui="yes", interactive={"claude": "native"}, slots=[])
    selected = setup.launcher.select(setup.config, "master", task)
    assert (selected.hive, selected.account.name, selected.route_kind) == ("login", "native", "interactive")


@pytest.mark.parametrize("ui", ["yes", "no"])
def test_master_admission_records_fallback_auth_and_headless_evidence(setup, ui):
    from scripts.swarm.tick import MASTER
    from scripts.swarm_v2.runtime.base import SpawnRequest

    task = master(setup)
    write_hive(setup, roles=["master"], ui=ui)
    name = setup.store.next_name(setup.config.slug, MASTER)
    task.update(seat="master@fixture", controller_epoch=setup.held.epoch)
    request = SpawnRequest(setup.config, MASTER, name, task)
    selected = setup.launcher.select(setup.config, MASTER, task)
    admitted = setup.launcher.admit(request, selected)
    assert admitted.desired_route_kind == "interactive"
    assert admitted.profile_decision["master_placement"] == {"hive": "remote", "headless": ui == "no"}
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
