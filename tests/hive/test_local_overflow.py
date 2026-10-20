from unittest.mock import Mock

import pytest

from scripts.hive.placement import LOCAL_MACHINE, Choice, Wait, overflow
from scripts.swarm.store import SwarmError
from scripts.swarm.tick import SpawnError
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive

FULL = "no hive for eng has room"
CAPPED = f"{FULL}, and the local machine runs 8 of its 8 agents"


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
    assert fenced.placement_refusal("eng", task) == CAPPED
    with pytest.raises(SpawnError, match=f"^{CAPPED}$") as raised:
        fenced.spawn(setup.config, "eng", "eng name", task)
    assert raised.value.status == "unavailable"
    setup.herdr.spawn.assert_not_called()
    fenced.runtime.hive_launch.spawn.assert_not_called()


def test_the_local_machine_cap_is_the_swarm_setting(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    local_agents(setup, 2)
    setup.store.update(setup.config.slug, local_max_agents=2)
    assert fenced.placement_refusal("eng", {"id": "task"}) == f"{FULL}, and the local machine runs 2 of its 2 agents"
    setup.store.update(setup.config.slug, local_max_agents=3)
    assert fenced.placement_refusal("eng", {"id": "task"}) == ""


def test_the_cap_counts_only_live_local_workers_of_every_swarm_but_not_the_task_itself(setup, monkeypatch):
    from scripts.swarm.store import AgentRecord, SwarmConfig

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    setup.store.update(setup.config.slug, local_max_agents=2)
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
    assert fenced.placement_refusal("eng", {"id": "task"}) == f"{FULL}, and the local machine runs 2 of its 2 agents"


def test_the_local_machine_cap_defaults_to_eight(setup):
    from scripts.swarm.store import SwarmConfig

    assert setup.store.config(setup.config.slug).local_max_agents == 8
    assert SwarmConfig("s", "r", 1, 1).local_max_agents == 8


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
    assert (selected, selected.role) == (Wait(FULL), "eng")


def test_the_local_spawn_refusal_is_the_exact_overflow_text(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    assert fenced._refusal("eng", {"id": "task"}) == "the local machine takes eng work"


@pytest.mark.parametrize(("profile", "refused"), [("engineer", ""), ("qa", "Codex hive placement is not enabled")])
def test_a_codex_worker_no_hive_can_launch_runs_locally_unless_its_role_needs_a_ui_host(
    setup, monkeypatch, profile, refused
):
    from scripts.swarm.profile_choice import ProfileDecision

    fenced = hybrid(setup, monkeypatch)
    setup.herdr._profile_decision.return_value = ProfileDecision(profile, "task", profile)
    task = {"id": "task", "launch_assignment": {"harness": "codex"}}
    assert fenced.placement_refusal("eng", task) == refused


def test_a_task_pinned_to_a_hive_never_moves_to_the_local_machine(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    assert fenced.placement_refusal("eng", {"id": "task", "hive": "anton"}) == FULL


@pytest.mark.parametrize("deployment", ["compose", "distributed"])
def test_only_the_workstation_controller_spawns_locally(setup, monkeypatch, deployment):
    fenced = hybrid(setup, monkeypatch, deployment)
    fill_anton(setup)
    assert fenced.placement_refusal("eng", {"id": "task"}) == FULL


def test_no_workstation_herdr_leaves_the_hive_wait(setup, monkeypatch):
    from scripts.swarm import controller

    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    monkeypatch.setattr(controller, "_workstation_herdr", lambda: False)
    assert fenced.placement_refusal("eng", {"id": "task"}) == FULL


def test_swarm_set_stores_the_local_machine_cap(setup, capsys):
    import argparse
    import json

    from scripts.swarm import cli

    setup.store.update(setup.config.slug, state="paused")
    cli.cmd_set(setup.store, argparse.Namespace(slug=setup.config.slug, pairs=["local-max-agents=5"]))
    assert setup.store.config(setup.config.slug).local_max_agents == 5
    assert json.loads(capsys.readouterr().out)["local_max_agents"] == 5
