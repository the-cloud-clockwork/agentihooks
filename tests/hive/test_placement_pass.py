import os
from unittest.mock import Mock

import pytest

from scripts.hive.launch import Selection
from scripts.swarm import controller
from scripts.swarm.tick import SpawnError
from tests.hive.test_controller_launch import request_for, setup, write_hive  # noqa: F401


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
        return herdr._profile_decision(config, "eng", {"id": task_id}, {}, {}, {}).bundle_revision

    assert herdr.pass_memo is None
    assert [revision("one"), revision("two")] == ["first", "second"]
    herdr.pass_memo = {}
    assert [revision("one"), revision("two")] == ["third", "third"]
    herdr.pass_memo = {}
    assert revision("one") == "fourth"


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
    assert "has room: remote" in result.detail
    assert seat_spawn.room_refused(SpawnError(result.detail, "unavailable"))
    waited = launch.select(setup.config, "eng", request.task)
    assert isinstance(waited, Wait)
    assert isinstance(launch.select(setup.config, "eng", {"id": "later"}), Wait)
    if in_pass:
        assert (waited.reason, waited.role) == (result.detail, role)
        rooms = {hive["id"]: hive["room"] for hive in launch.hives()}
        assert rooms["remote"] == 0
        assert rooms["spare"] > 0
