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
    setup.herdr.spawn.assert_not_called()


def test_preselected_root_requests_share_the_last_configured_slot(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", max_agents=8)
    setup.store.update(setup.config.slug, local_max_agents=2)
    local_agents(setup, 1)
    selected = SimpleNamespace(hive="workstation", master_placement=None)
    first = SimpleNamespace(name="first", lane="eng", task={"id": "first"})
    second = SimpleNamespace(name="second", lane="eng", task={"id": "second"})
    assert setup.launcher.reserve_room(first, selected) == ""
    assert "2 of its 2" in setup.launcher.reserve_room(second, selected)


def test_a_root_reservation_consumes_the_same_slot_as_overflow(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=1)
    selected = SimpleNamespace(hive="workstation", master_placement=None)
    request = SimpleNamespace(name="first", lane="eng", task={"id": "first"})
    assert setup.launcher.reserve_room(request, selected) == ""
    setup.herdr._quota_accounts = []
    with pytest.raises(SpawnError, match="the local machine runs"):
        fenced.spawn(setup.config, "eng", "second", {"id": "second"})


def test_local_deployment_obeys_the_workstation_cap(setup, monkeypatch):
    from scripts.swarm.controller import FencedRuntime

    fenced = FencedRuntime(setup.store, setup.config.slug, setup.held, setup.herdr, "local")
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


def frontend_root(setup, monkeypatch, limit):
    from scripts.swarm.profile_choice import ProfileDecision

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=8)
    setup.herdr._profile_decision.return_value = ProfileDecision("frontend", "task", "frontend")
    return setup.store.update(setup.config.slug, local_max_agents=limit)


def test_root_selection_counts_pending_slots_and_reports_the_swarm_limit(setup, monkeypatch):
    from scripts.swarm import local_slots

    config = frontend_root(setup, monkeypatch, 1)
    local_slots.claim(setup.store, "workstation", setup.config.slug, "busy", "pending", 8)
    selected = setup.launcher.select(config, "eng", {"id": "task", "hive": "workstation"})
    assert isinstance(selected, Wait)
    assert "max agents: 1 of 1" in selected.reason


def test_root_selection_excludes_its_own_existing_task(setup, monkeypatch):
    from scripts.hive.launch import Selection
    from scripts.swarm.store import AgentRecord

    config = frontend_root(setup, monkeypatch, 1)
    setup.store.put_agent(setup.config.slug, AgentRecord("self", "eng", "task"))
    selected = setup.launcher.select(config, "eng", {"id": "task", "hive": "workstation"})
    assert isinstance(selected, Selection)
    assert selected.hive == "workstation"


def test_root_selection_preserves_a_hive_limit_refusal(setup, monkeypatch):
    from scripts.swarm.store import AgentRecord

    config = frontend_root(setup, monkeypatch, 8)
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=1)
    setup.store.put_agent(setup.config.slug, AgentRecord("busy", "eng", "other"))
    selected = setup.launcher.select(config, "eng", {"id": "task", "hive": "workstation"})
    assert isinstance(selected, Wait)
    assert "max agents: 1 of 1" in selected.reason


def test_a_root_resource_refusal_releases_only_its_local_claim(setup, monkeypatch):
    from scripts.hive import admission
    from scripts.swarm import local_slots
    from tests.hive.test_controller_launch import publish_room, request_for

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", max_agents=8)
    publish_room(setup.store, "workstation", 1)
    admission.reserve(setup.store.redis, "workstation", "other/pending", 1)
    request = request_for(setup)
    selected = SimpleNamespace(hive="workstation", master_placement=None)
    assert "ram:" in setup.launcher.reserve_room(request, selected)
    assert setup.store.redis.hgetall(local_slots.key("workstation")) == {}
    assert admission.pending(setup.store.redis, "workstation") == 1


def test_an_existing_task_cannot_inflate_resource_room_during_a_pending_launch(setup, monkeypatch):
    from scripts.swarm import local_slots
    from scripts.swarm.store import AgentRecord
    from tests.hive.test_controller_launch import publish_room

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", max_agents=8)
    publish_room(setup.store, "workstation", 1)
    setup.store.put_agent(setup.config.slug, AgentRecord("self", "eng", "task"))
    local_slots.claim(setup.store, "workstation", setup.config.slug, "other", "pending", 8)
    selected = SimpleNamespace(hive="workstation", master_placement=None)
    request = SimpleNamespace(name="self", lane="eng", task={"id": "task"})
    assert "1 of its 1" in setup.launcher.reserve_room(request, selected)


def test_local_mode_exempts_masters_at_capacity(setup, monkeypatch):
    from scripts.swarm.controller import FencedRuntime

    fenced = FencedRuntime(setup.store, setup.config.slug, setup.held, setup.herdr, "local")
    local_agents(setup, 8)
    assert fenced.placement_refusal("master", {"id": "master"}) == ""
    assert fenced.spawn(setup.config, "master", "master-new", {"id": "master"}) == setup.herdr.spawn.return_value


def test_local_mode_excludes_its_own_task_and_honors_pending_claims(setup, monkeypatch):
    from scripts.swarm import local_slots
    from scripts.swarm.controller import FencedRuntime
    from scripts.swarm.store import AgentRecord

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    setup.store.update(setup.config.slug, local_max_agents=1)
    setup.store.put_agent(setup.config.slug, AgentRecord("self", "eng", "task"))
    fenced = FencedRuntime(setup.store, setup.config.slug, setup.held, setup.herdr, "local")
    assert fenced.placement_refusal("eng", {"id": "task"}) == ""
    local_slots.claim(setup.store, "workstation", setup.config.slug, "other", "pending", 8)
    assert "local machine runs 1 of its 1" in fenced.placement_refusal("eng", {"id": "task"})


def test_a_local_spawn_reserves_its_own_machine_and_task_once(setup, monkeypatch):
    from scripts.hive import admission
    from scripts.swarm import local_slots
    from scripts.swarm.controller import FencedRuntime
    from scripts.swarm.store import AgentRecord

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    setup.store.update(setup.config.slug, local_max_agents=1)
    setup.store.put_agent(setup.config.slug, AgentRecord("self", "eng", "task"))
    fenced = FencedRuntime(setup.store, setup.config.slug, setup.held, setup.herdr, "local")
    assert fenced.spawn(setup.config, "eng", "self", {"id": "task"}) == setup.herdr.spawn.return_value
    assert set(setup.store.redis.hgetall(local_slots.key("workstation"))) == {f"{setup.config.slug}/self"}
    assert admission.pending(setup.store.redis, "workstation") == 1
    assert local_slots.held(setup.store, "workstation", setup.config.slug, "other") == 1


@pytest.mark.parametrize("route", ["subscription", "api"])
@pytest.mark.parametrize("hive_limit", [1, 8])
def test_an_explicit_route_master_ignores_worker_caps_but_reserves_resources(setup, monkeypatch, route, hive_limit):
    from scripts.hive import admission
    from scripts.hive.launch import Selection
    from scripts.swarm import local_slots
    from scripts.swarm.profile_choice import ProfileDecision

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=hive_limit)
    config = setup.store.update(setup.config.slug, local_max_agents=1)
    local_agents(setup, 1)
    setup.herdr._profile_decision.return_value = ProfileDecision("master", "task", "master")
    task = {"id": "master", "hive": "workstation", "route_kind": route}
    selected = setup.launcher.select(config, "master", task)
    assert isinstance(selected, Selection)
    assert selected.master_placement is None
    assert setup.launcher.reserve_room(SimpleNamespace(name="master", lane="master", task=task), selected) == ""
    assert setup.store.redis.hgetall(local_slots.key("workstation")) == {}
    assert admission.pending(setup.store.redis, "workstation") == 1


def local_fenced(setup, monkeypatch, cpus, load5=0.0):
    from scripts.hive import headroom
    from scripts.swarm.controller import FencedRuntime

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    reading = {"cpus": cpus, "load5": load5, "available_mb": 10**6, "total_mb": 10**6}
    monkeypatch.setattr(headroom, "measure", lambda pending, now, previous: reading)
    return FencedRuntime(setup.store, setup.config.slug, setup.held, setup.herdr, "local")


@pytest.mark.parametrize("lane", ["eng", "ci", "plan"])
def test_a_local_launch_over_the_swarm_share_is_refused_with_its_reason(setup, monkeypatch, lane):
    fenced = local_fenced(setup, monkeypatch, 3)
    local_agents(setup, 2)
    reason = (
        ", and the local machine runs 2 of its 2 agents: cpu: 2 swarm agents at 0.5 CPU each on 3 CPUs, "
        "fits 0 more under the 40 percent swarm share"
    )
    assert fenced.placement_refusal(lane, {"id": "task"}) == reason
    with pytest.raises(SpawnError) as raised:
        fenced.spawn(setup.config, lane, "next", {"id": "task"})
    assert (str(raised.value), raised.value.status) == (reason, "unavailable")
    setup.herdr.spawn.assert_not_called()


def test_a_quiet_workstation_admits_past_the_swarm_share_up_to_the_root_hive_max_agents(setup, monkeypatch):
    fenced = local_fenced(setup, monkeypatch, 16, load5=4.32)
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=20)
    setup.store.update(setup.config.slug, local_max_agents=20)
    local_agents(setup, 12)
    assert fenced.placement_refusal("eng", {"id": "task"}) == ""
    assert fenced.spawn(setup.config, "eng", "next", {"id": "task"}) == setup.herdr.spawn.return_value


def test_a_local_launch_over_the_host_line_is_refused_when_it_is_tighter_than_the_share(setup, monkeypatch):
    fenced = local_fenced(setup, monkeypatch, 10, load5=8.0)
    with pytest.raises(SpawnError) as raised:
        fenced.spawn(setup.config, "eng", "next", {"id": "task"})
    assert str(raised.value) == (
        ", and the local machine runs 0 of its 0 agents: cpu: five minute load 8.00 on 10 CPUs, "
        "0.5 CPU per agent, fits 0 more under the 80 percent line"
    )
    setup.herdr.spawn.assert_not_called()


def test_an_over_budget_local_master_still_starts_and_counts_against_the_workstation(setup, monkeypatch):
    from scripts.hive import admission

    fenced = local_fenced(setup, monkeypatch, 10, load5=8.0)
    assert fenced.placement_refusal("master", {"id": "master"}) == ""
    assert fenced.spawn(setup.config, "master", "master-new", {"id": "master"}) == setup.herdr.spawn.return_value
    assert admission.pending(setup.store.redis, "workstation") == 1
    assert fenced.placement_refusal("eng", {"id": "task"}).endswith(
        "0.5 CPU per agent with 1 pending launches, fits 0 more under the 80 percent line"
    )


def test_a_hybrid_master_falling_back_to_the_workstation_counts_against_it(setup, monkeypatch):
    from scripts.hive import admission

    fenced = hybrid(setup, monkeypatch)
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    assert fenced.spawn(setup.config, "master", "master-new", {"id": "master"}) == "placed on the workstation"
    assert admission.pending(setup.store.redis, "workstation") == 1
