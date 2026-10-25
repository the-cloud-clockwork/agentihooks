import json
from unittest.mock import Mock

import pytest

from scripts.hive.placement import LOCAL_MACHINE, Choice, Wait, overflow
from scripts.swarm.store import SwarmError
from scripts.swarm.tick import SpawnError
from tests.hive.test_controller_launch import publish_room, write_hive
from tests.hive.test_controller_launch import setup as setup

FULL = "no hive for eng has room"
ANTON = "anton ram: 20000 of 100000 MB available, 700 MB per agent, fits 0 more under the 80 percent line"
HIVE_FULL = f"{FULL}: {ANTON}"


def capped(held, of, running, cpus=10):
    return (
        f"{HIVE_FULL}, and the local machine runs {held} of its {of} agents: cpu: {running} swarm agents at 0.5 CPU "
        f"each on {cpus} CPUs, fits {of - running} more under the 40 percent swarm share"
    )


def workstation(monkeypatch, cpus):
    from scripts.hive import headroom

    reading = {"cpus": cpus, "load5": 0.0, "available_mb": 10**6, "total_mb": 10**6, "pending": 0, "measured_at": 0}
    monkeypatch.setattr(headroom, "measure", lambda pending, now, previous: reading)


@pytest.mark.parametrize("role", ["engineer", "cicd", "planner", "dispatcher", ""])
def test_a_worker_no_hive_takes_runs_on_the_local_machine_under_its_cap(role):
    assert overflow(role, Wait(FULL), 7, 8) == Choice(LOCAL_MACHINE, FULL)
    assert overflow(role, Wait(FULL), 0, 1) == Choice(LOCAL_MACHINE, FULL)


@pytest.mark.parametrize("agents", [8, 9])
def test_a_local_machine_at_its_cap_leaves_the_worker_waiting_and_says_so(agents):
    assert overflow("engineer", Wait(FULL), agents, 8) == Wait(
        f"{FULL}, and the local machine runs {agents} of its 8 agents"
    )


@pytest.mark.parametrize("role", ["master", "qa", "frontend"])
def test_ui_roles_keep_their_ui_host_rule(role):
    assert overflow(role, Wait(FULL), 0, 8) == Wait(FULL)


def hybrid(setup, monkeypatch, deployment="hybrid"):
    from scripts.hive import registry
    from scripts.swarm import controller
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    monkeypatch.setattr(controller, "_workstation_herdr", lambda: True)
    workstation(monkeypatch, 10)
    setup.store.redis.delete(registry.key("remote"))
    setup.store.redis.srem(registry.INDEX, "remote")
    write_hive(setup, id="anton", ui="no", excluded=["master", "qa", "frontend"], max_agents=2)
    fenced = controller.FencedRuntime(
        setup.store, setup.config.slug, setup.held, RoutedRuntime(setup.herdr, RuntimeRouter([])), deployment
    )
    fenced.runtime.hive_launch.spawn = Mock(return_value=Mock(ok=True, value="placed on the hive"))
    setup.herdr.spawn.return_value = "placed on the workstation"
    return fenced


def fill_anton(setup):
    from scripts.hive import registry

    setup.store.redis.set(f"{registry.key('anton')}:sessions", '{"eng": 2}')
    publish_room(setup.store, "anton", 0)


def local_agents(setup, count, slug=None, **fields):
    from scripts.swarm.store import AgentRecord

    for index in range(count):
        setup.store.put_agent(slug or setup.config.slug, AgentRecord(f"eng-{index}", "eng", f"busy-{index}", **fields))


def test_a_spawn_goes_to_a_headless_hive_with_room_first(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    task = {"id": "task", "seat": "eng-1@fixture"}
    assert fenced.placement_refusal("eng", task) == ""
    assert fenced.spawn(setup.config, "eng", "eng name", task) == "placed on the hive"
    setup.herdr.spawn.assert_not_called()


def test_a_spawn_no_hive_takes_falls_back_to_the_local_machine(setup, monkeypatch):
    from scripts.hive import placement_waits

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    local_agents(setup, 7)
    task = {"id": "task", "seat": "eng-1@fixture"}
    placement_waits.observe(setup.store, setup.config.slug, "eng", "other", FULL, 1)
    assert fenced.placement_refusal("eng", task) == ""
    assert list(placement_waits.read(setup.store, setup.config.slug)) == ["other"]
    assert fenced.spawn(setup.config, "eng", "eng name", task) == "placed on the workstation"
    _, lane, name, sent = setup.herdr.spawn.call_args.args
    assert (lane, name, sent["id"], sent["controller_epoch"]) == ("eng", "eng name", "task", setup.held.epoch)
    fenced.runtime.hive_launch.spawn.assert_not_called()


def test_the_local_machine_cap_holds_the_spawn_with_the_reason(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    local_agents(setup, 8)
    task = {"id": "task", "seat": "eng-1@fixture"}
    assert fenced.placement_refusal("eng", task) == capped(8, 8, 8)
    with pytest.raises(SpawnError) as raised:
        fenced.spawn(setup.config, "eng", "eng name", task)
    assert (str(raised.value), raised.value.status) == (capped(8, 8, 8), "unavailable")
    setup.herdr.spawn.assert_not_called()
    fenced.runtime.hive_launch.spawn.assert_not_called()


def local_refusal(setup, since=0):
    from scripts.swarm.commands import hive_id
    from scripts.swarm.health import spawn_stall

    found = json.loads(setup.store.redis.get(setup.store.key(setup.config.slug, spawn_stall.LOCAL_REFUSAL)))
    assert found.pop("machine") == hive_id()
    assert found.pop("at") >= since
    return found


@pytest.mark.parametrize("deployment", ["hybrid", "local"])
def test_a_local_cap_refusal_is_recorded_for_the_spawn_stall_alarm(setup, monkeypatch, deployment):
    from scripts.swarm import controller, lease
    from scripts.swarm.health import spawn_stall

    if deployment == "local":
        fenced = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, setup.herdr, deployment)
    else:
        fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    local_agents(setup, 8)
    since = lease.now_ms(setup.store)
    assert "local machine runs 8 of its 8 agents" in fenced.placement_refusal("eng", {"id": "task"})
    assert local_refusal(setup, since) == {"taken": 8, "maximum": 8}
    assert spawn_stall.LOCAL_REFUSAL == "spawn-stall-local"


def test_a_hive_refusal_alone_records_no_local_cap(setup, monkeypatch):
    from scripts.swarm.health import spawn_stall

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    local_agents(setup, 3)
    assert "local machine runs" not in fenced.placement_refusal("eng", {"id": "task"})
    assert setup.store.redis.get(setup.store.key(setup.config.slug, spawn_stall.LOCAL_REFUSAL)) is None


def test_the_local_machine_cap_is_its_swarm_cpu_share(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    local_agents(setup, 2)
    workstation(monkeypatch, 3)
    assert fenced.placement_refusal("eng", {"id": "task"}) == capped(2, 2, 2, cpus=3)
    workstation(monkeypatch, 4)
    assert fenced.placement_refusal("eng", {"id": "task"}) == ""


def test_the_cap_counts_only_live_local_workers_of_every_swarm_but_not_the_task_itself(setup, monkeypatch):
    from scripts.swarm.store import AgentRecord, SwarmConfig

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    workstation(monkeypatch, 3)
    setup.store.create(SwarmConfig("other", "agentihooks", 1, 1))
    local_agents(setup, 1, slug="other")
    stored = setup.store.agents
    others = [
        AgentRecord("master-1", "master", "master"),
        AgentRecord("eng-hive", "eng", "on-hive", hive="anton"),
        AgentRecord("eng-pod", "eng", "on-pod", runtime_backend="kubernetes"),
        AgentRecord("eng-done", "eng", "done", state="finished"),
        AgentRecord("eng-self", "eng", "task", state="starting"),
    ]
    agents = {"other": stored("other"), setup.config.slug: others}
    monkeypatch.setattr(setup.store, "agents", lambda slug: agents.get(slug, []))
    assert fenced.placement_refusal("eng", {"id": "task"}) == ""
    agents[setup.config.slug] = [*others, AgentRecord("eng-1", "eng", "busy")]
    assert fenced.placement_refusal("eng", {"id": "task"}) == capped(2, 2, 2, cpus=3)


def test_the_local_machine_cap_defaults_to_eight(setup):
    from scripts.swarm.store import SwarmConfig

    assert setup.store.config(setup.config.slug).local_max_agents == 8
    assert SwarmConfig("s", "r", 1, 1).local_max_agents == 8


def test_the_swarm_setting_refuses_local_launches_even_with_spare_cpu(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    setup.store.update(setup.config.slug, local_max_agents=2)
    local_agents(setup, 2)
    with pytest.raises(SpawnError, match="the local machine runs"):
        fenced.spawn(setup.config, "eng", "eng-new", {"id": "task"})
    setup.herdr.spawn.assert_not_called()


def test_the_workstation_hive_limit_counts_another_swarm(setup, monkeypatch):
    from scripts.swarm import commands
    from scripts.swarm.store import SwarmConfig

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=2)
    setup.store.create(SwarmConfig("doctor", "agentihooks", 1, 1))
    local_agents(setup, 2, slug="doctor")
    with pytest.raises(SpawnError, match="the local machine runs"):
        fenced.spawn(setup.config, "eng", "eng-new", {"id": "task"})
    setup.herdr.spawn.assert_not_called()


@pytest.mark.parametrize("lane", ["eng", "ci"])
def test_codex_local_fallback_refuses_at_the_eight_agent_cap(setup, monkeypatch, lane):
    from scripts.swarm.store import SwarmConfig

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    workstation(monkeypatch, 40)
    setup.store.create(SwarmConfig("other-swarm", "agentihooks", 1, 1))
    local_agents(setup, 8, slug="other-swarm", harness="codex")
    task = {"id": "task", "seat": f"{lane}-1@fixture", "launch_assignment": {"harness": "codex"}}
    assert "local machine runs 8 of its 8 agents" in fenced.placement_refusal(lane, task)
    with pytest.raises(SpawnError, match="local machine runs 8 of its 8 agents"):
        fenced.spawn(setup.config, lane, "eng name", task)
    setup.herdr.spawn.assert_not_called()
    fenced.runtime.hive_launch.spawn.assert_not_called()


@pytest.mark.parametrize("profile", ["qa", "frontend"])
def test_ui_role_work_keeps_waiting_for_its_ui_host(setup, monkeypatch, profile):
    from scripts.swarm.profile_choice import ProfileDecision

    fenced = hybrid(setup, monkeypatch)
    setup.herdr._profile_decision.return_value = ProfileDecision(profile, "task", profile)
    assert fenced.placement_refusal("eng", {"id": "task"}) == f"no live hive takes {profile}"
    with pytest.raises(SwarmError, match=f"^no live hive takes {profile}$"):
        fenced.spawn(setup.config, "eng", "eng name", {"id": "task"})
    setup.herdr.spawn.assert_not_called()


def test_the_role_comes_from_the_hive_profile_decision_not_the_task_fields(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    task = {"id": "task", "profile": "qa", "profile_decision": {"profile": "qa"}}
    assert fenced.placement_refusal("eng", task) == ""
    selected = fenced.runtime.hive_launch.select(setup.config, "eng", task)
    assert (selected, selected.role) == (Wait(HIVE_FULL), "eng")


def test_the_local_spawn_refusal_is_the_exact_overflow_text(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    assert fenced._refusal("eng", {"id": "task"}) == "the local machine takes eng work"


@pytest.mark.parametrize(("profile", "refused"), [("engineer", ""), ("qa", "no live hive takes qa")])
def test_a_codex_worker_no_hive_can_launch_runs_locally_unless_its_role_needs_a_ui_host(
    setup, monkeypatch, profile, refused
):
    from scripts.swarm.capacity import Account
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._quota_accounts.append(Account("codex", "account", "room", 0, None, 80, 6))
    fenced = hybrid(setup, monkeypatch)
    setup.herdr._profile_decision.return_value = ProfileDecision(profile, "task", profile)
    task = {"id": "task", "launch_assignment": {"harness": "codex"}}
    assert fenced.placement_refusal("eng", task) == refused


def test_a_task_pinned_to_a_hive_never_moves_to_the_local_machine(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    assert fenced.placement_refusal("eng", {"id": "task", "hive": "anton"}) == HIVE_FULL


@pytest.mark.parametrize("deployment", ["compose", "distributed"])
def test_only_the_workstation_controller_spawns_locally(setup, monkeypatch, deployment):
    fenced = hybrid(setup, monkeypatch, deployment)
    fill_anton(setup)
    assert fenced.placement_refusal("eng", {"id": "task"}) == HIVE_FULL


def test_no_workstation_herdr_leaves_the_hive_wait(setup, monkeypatch):
    from scripts.swarm import controller

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    monkeypatch.setattr(controller, "_workstation_herdr", lambda: False)
    assert fenced.placement_refusal("eng", {"id": "task"}) == HIVE_FULL


@pytest.mark.parametrize("excluded", ["eng", "ci"])
def test_a_lane_or_role_the_local_hive_excludes_waits_for_a_hive_instead_of_running_locally(
    setup, monkeypatch, excluded
):
    from scripts.swarm.profile_choice import ProfileDecision

    fenced = hybrid(setup, monkeypatch)
    setup.herdr._profile_decision.return_value = ProfileDecision("cicd", "task", "cicd")
    fill_anton(setup)
    write_hive(setup, id="workstation", ui="yes", excluded=[excluded], max_agents=0)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "workstation")
    assert fenced.placement_refusal("eng", {"id": "task"}) == f"no hive for ci has room: {ANTON}"


def test_a_lane_the_local_hive_keeps_still_runs_locally_when_no_hive_has_room(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    write_hive(setup, id="workstation", ui="yes", excluded=["master"], max_agents=1)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "workstation")
    assert fenced.placement_refusal("eng", {"id": "task"}) == ""


def test_swarm_set_stores_the_local_machine_cap(setup, capsys):
    import argparse
    import json

    from scripts.swarm import cli

    setup.store.update(setup.config.slug, state="paused")
    cli.cmd_set(setup.store, argparse.Namespace(slug=setup.config.slug, pairs=["local-max-agents=5"]))
    assert setup.store.config(setup.config.slug).local_max_agents == 5
    assert json.loads(capsys.readouterr().out)["local_max_agents"] == 5


def test_two_concurrent_spawns_at_cap_minus_one_yield_one_local_spawn_and_one_wait(setup, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from scripts.swarm import local_slots

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    local_agents(setup, 7)
    monkeypatch.setattr(local_slots, "held", lambda *_: 0)
    read, together, first_reads = local_slots._held, threading.Barrier(2), iter(range(2))

    def read_together(*args):
        seen = read(*args)
        if next(first_reads, None) is not None:
            together.wait(5)
        return seen

    monkeypatch.setattr(local_slots, "_held", read_together)

    def attempt(task):
        try:
            return fenced.spawn(setup.config, "eng", f"eng {task}", {"id": task})
        except SpawnError as refused:
            return str(refused)

    with ThreadPoolExecutor(2) as pool:
        outcomes = sorted(pool.map(attempt, ["first", "second"]))
    assert outcomes == [capped(8, 8, 7), "placed on the workstation"]
    assert setup.herdr.spawn.call_count == 1
    assert local_refusal(setup) == {"taken": 8, "maximum": 8}


def test_a_failed_local_spawn_releases_its_slot(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    local_agents(setup, 7)
    setup.herdr.spawn.side_effect = SpawnError("pane refused", "unavailable")
    with pytest.raises(SpawnError, match="^pane refused$"):
        fenced.spawn(setup.config, "eng", "eng first", {"id": "first"})
    setup.herdr.spawn.side_effect = None
    assert fenced.spawn(setup.config, "eng", "eng second", {"id": "second"}) == "placed on the workstation"


def test_pending_local_launches_hold_the_host_line_until_the_load_shows_them(setup, monkeypatch):
    from scripts.hive import admission, headroom
    from scripts.swarm.store import AgentRecord

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "box")
    reading = {"cpus": 10, "load5": 6.0, "available_mb": 10**6, "total_mb": 10**6}
    monkeypatch.setattr(headroom, "measure", lambda pending, now, previous: reading)
    for index in range(4):
        assert fenced.spawn(setup.config, "eng", f"eng {index}", {"id": f"t{index}"}) == "placed on the workstation"
    assert admission.pending(setup.store.redis, "box") == 4
    for index in range(4):
        setup.store.put_agent(setup.config.slug, AgentRecord(f"eng {index}", "eng", f"t{index}"))
    assert fenced.placement_refusal("eng", {"id": "t4"}) == (
        f"{HIVE_FULL}, and the local machine runs 4 of its 4 agents: cpu: five minute load 6.00 on 10 CPUs, "
        "0.5 CPU per agent with 4 pending launches, fits 0 more under the 80 percent line"
    )
    assert setup.herdr.spawn.call_count == 4


def test_a_failed_local_spawn_releases_its_reservation(setup, monkeypatch):
    from scripts.hive import admission

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "box")
    setup.herdr.spawn.side_effect = SpawnError("pane refused", "unavailable")
    with pytest.raises(SpawnError, match="^pane refused$"):
        fenced.spawn(setup.config, "eng", "eng first", {"id": "first"})
    assert admission.pending(setup.store.redis, "box") == 0


def test_the_host_line_leaves_out_the_task_itself_and_measures_without_pending(setup, monkeypatch):
    from scripts.hive import headroom
    from scripts.swarm import lease
    from scripts.swarm.store import AgentRecord

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "box")
    published = {"cpus": 10, "load5": 1.0, "pod_cpu_usec": 7, "measured_at": lease.now_ms(setup.store)}
    setup.store.redis.set(headroom.key("box"), json.dumps(published))
    measured = []
    reading = {"cpus": 10, "load5": 8.0, "available_mb": 10**6, "total_mb": 10**6}
    monkeypatch.setattr(
        headroom, "measure", lambda pending, now, previous: measured.append((pending, now, previous)) or reading
    )
    local_agents(setup, 4)
    setup.store.put_agent(setup.config.slug, AgentRecord("eng-self", "eng", "task"))
    assert fenced.placement_refusal("eng", {"id": "task"}) == (
        f"{HIVE_FULL}, and the local machine runs 4 of its 4 agents: cpu: five minute load 8.00 on 10 CPUs, "
        "0.5 CPU per agent, fits 0 more under the 80 percent line"
    )
    assert [pending for pending, _, _ in measured] == [0] * len(measured)
    assert all(abs(now - lease.now_ms(setup.store)) < 60_000 for _, now, _ in measured)
    assert [previous for _, _, previous in measured] == [published] * len(measured) != []


def test_a_slot_counts_once_while_its_agent_runs_and_not_after_it_leaves(setup):
    from scripts.swarm import local_slots
    from scripts.swarm.store import AgentRecord

    slug = setup.config.slug
    setup.store.put_agent(slug, AgentRecord("eng-1", "eng", "first", state="starting"))
    assert local_slots.held(setup.store, "box", slug, "other") == 0
    assert local_slots.claim(setup.store, "box", slug, "first", "eng-1", 8) == 0
    assert local_slots.held(setup.store, "box", slug, "other") == 1
    assert local_slots.held(setup.store, "box", slug, "first") == 0
    assert local_slots.held(setup.store, "elsewhere", slug, "other") == 0
    setup.store.put_agent(slug, AgentRecord("eng-1", "eng", "first"))
    assert local_slots.held(setup.store, "box", slug, "other") == 1
    assert local_slots.held(setup.store, "box", slug, "first") == 0
    assert local_slots.claim(setup.store, "box", slug, "second", "eng-2", 8) == 1
    assert setup.store.redis.hkeys(local_slots.key("box")) == [f"{slug}/eng-2"]
    setup.store.put_agent(slug, AgentRecord("eng-1", "eng", "first", state="finished"))
    assert local_slots.held(setup.store, "box", slug, "other") == 1
    setup.store.put_agent(slug, AgentRecord("eng-2", "eng", "second", state="finished"))
    assert local_slots.held(setup.store, "box", slug, "other") == 0


def test_a_claim_whose_agent_never_appears_expires(setup):
    from scripts.swarm import lease, local_slots
    from scripts.swarm.store import AgentRecord

    slug = setup.config.slug
    now = lease.now_ms(setup.store)
    claims = {f"{slug}/old": now - local_slots.PENDING_MS, f"{slug}/new": now}
    setup.store.redis.hset(local_slots.key("box"), mapping=claims)
    assert local_slots.held(setup.store, "box", slug, "other") == 1
    assert local_slots.claim(setup.store, "box", slug, "third", "eng-3", 2) == 1
    assert local_slots.claim(setup.store, "box", slug, "fourth", "eng-4", 2) == 2
    assert sorted(setup.store.redis.hkeys(local_slots.key("box"))) == [f"{slug}/eng-3", f"{slug}/new"]
    local_slots.release(setup.store, "box", slug, "eng-3")
    assert local_slots.held(setup.store, "box", slug, "other") == 1
    setup.store.put_agent(slug, AgentRecord("stuck", "eng", "stuck", state="starting"))
    setup.store.redis.hset(local_slots.key("box"), f"{slug}/stuck", now - local_slots.PENDING_MS)
    assert local_slots.held(setup.store, "box", slug, "other") == 1


def test_the_machine_count_leaves_out_the_task_itself_and_holds_other_swarms_pending_claims(setup, monkeypatch):
    from scripts.swarm import local_slots
    from scripts.swarm.store import AgentRecord

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "box")
    workstation(monkeypatch, 3)
    setup.store.put_agent(setup.config.slug, AgentRecord("eng-self", "eng", "task"))
    assert local_slots.claim(setup.store, "box", "other", "elsewhere", "eng-other", 8) == 1
    assert fenced.placement_refusal("eng", {"id": "task"}) == ""
    assert fenced.spawn(setup.config, "eng", "eng name", {"id": "task"}) == "placed on the workstation"
    assert fenced.placement_refusal("eng", {"id": "next"}) == capped(3, 2, 1, cpus=3)
