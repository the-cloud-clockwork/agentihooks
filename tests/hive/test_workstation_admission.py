from types import SimpleNamespace

import pytest

from scripts.hive.placement import Wait
from scripts.swarm import commands
from scripts.swarm.tick import SpawnError
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive
from tests.hive.test_local_overflow import fill_anton, hybrid, local_agents


def test_successful_root_selection_still_obeys_the_swarm_limit(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", max_agents=8)
    config = setup.store.update(setup.config.slug, local_max_agents=2)
    local_agents(setup, 2)
    selected = setup.launcher.select(config, "eng", {"id": "task", "hive": "workstation"})
    assert isinstance(selected, Wait)
    assert "2 of 2" in selected.reason


def test_preselected_root_requests_share_the_last_configured_slot(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", max_agents=8)
    setup.store.update(setup.config.slug, local_max_agents=2)
    local_agents(setup, 1)
    selected = SimpleNamespace(hive="workstation", master_placement=None)
    first = SimpleNamespace(name="first", task={"id": "first"})
    second = SimpleNamespace(name="second", task={"id": "second"})
    assert setup.launcher.reserve_room(first, selected) == ""
    assert "2 of its 2" in setup.launcher.reserve_room(second, selected)


def test_a_root_reservation_consumes_the_same_slot_as_overflow(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=1)
    selected = SimpleNamespace(hive="workstation", master_placement=None)
    request = SimpleNamespace(name="first", task={"id": "first"})
    assert setup.launcher.reserve_room(request, selected) == ""
    setup.herdr._quota_accounts = []
    with pytest.raises(SpawnError, match="the local machine runs"):
        fenced.spawn(setup.config, "eng", "second", {"id": "second"})


def test_local_deployment_obeys_the_workstation_cap(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch, deployment="local")
    local_agents(setup, 8)
    with pytest.raises(SpawnError, match="the local machine runs"):
        fenced.spawn(setup.config, "ci", "next", {"id": "task"})


def test_failed_root_publication_releases_the_shared_local_reservation(setup, monkeypatch):
    from scripts.hive import launch
    from scripts.swarm import local_slots
    from tests.hive.test_controller_launch import request_for

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", max_agents=1)
    request = request_for(setup)
    selected = launch.Selection(
        setup.herdr._profile_decision.return_value, "workstation", setup.herdr._quota_accounts[0], "subscription"
    )
    monkeypatch.setattr(setup.launcher, "select", lambda *_: selected)
    setup.launcher.send_order = lambda *_: (_ for _ in ()).throw(RuntimeError("publication failed"))
    with pytest.raises(RuntimeError, match="publication failed"):
        setup.launcher.spawn(request)
    assert setup.store.redis.hgetall(local_slots.key("workstation")) == {}


def test_ending_a_root_launch_releases_its_shared_local_slot(setup, monkeypatch):
    from scripts.hive import launch
    from scripts.swarm import local_slots
    from scripts.swarm.store import AgentRecord

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    local_slots.claim(setup.store, "workstation", setup.config.slug, "task", "first", 1)
    launch.release(setup.store, setup.config.slug, AgentRecord("first", "eng", "task", hive="workstation"))
    assert setup.store.redis.hgetall(local_slots.key("workstation")) == {}
