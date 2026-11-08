from types import SimpleNamespace

import pytest

from scripts.hive import launch, placement, placement_waits
from scripts.hive.placement import Wait
from scripts.swarm import commands, local_slots
from scripts.swarm.tick import SpawnError
from scripts.swarm_v2.runtime.base import LOCAL, Placement
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive
from tests.hive.test_local_overflow import hybrid

TASK = {"id": "landing", "seat": "eng-1@fixture", "workstation": True}


def test_every_remote_hive_refuses_a_workstation_task_with_a_waits_line(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, id="anton", ui="no", max_agents=4)
    assert isinstance(setup.launcher.select(setup.config, "eng", {"id": "other"}), launch.Selection)
    selected = setup.launcher.select(setup.config, "eng", TASK)
    assert selected == Wait(placement.NEEDS_WORKSTATION)
    assert selected.role == "eng"
    row = placement_waits.read(setup.store, setup.config.slug)["landing"]
    assert (row["lane"], row["reason"]) == ("eng", placement.NEEDS_WORKSTATION)


def test_a_workstation_task_selects_the_workstation_hive_over_a_preferred_remote(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, prefer={"eng": 0})
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=8)
    assert setup.launcher.select(setup.config, "eng", {"id": "other"}).hive == "remote"
    assert setup.launcher.select(setup.config, "eng", TASK).hive == "workstation"


def test_a_hybrid_controller_spawns_a_workstation_task_on_the_local_machine(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    fenced = hybrid(setup, monkeypatch)
    assert fenced.placement_refusal("eng", TASK) == ""
    assert fenced.spawn(setup.config, "eng", "eng name", TASK) == "placed on the workstation"
    fenced.runtime.hive_launch.spawn.assert_not_called()


def test_runtime_placement_keeps_a_workstation_task_local():
    placed = Placement("kubernetes")
    assert placed.backend_for("eng", {"id": "other"}) == "kubernetes"
    assert placed.backend_for("eng", TASK) == LOCAL
    assert placed.backend_for("eng", {**TASK, "workstation": False}) == "kubernetes"


def test_a_hybrid_controller_claims_a_local_slot_for_a_workstation_task_on_the_workstation_hive(
    setup, monkeypatch, capsys
):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    fenced = hybrid(setup, monkeypatch)
    fenced.runtime.router.runtimes[LOCAL] = LocalHerdrRuntime(setup.herdr)
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=4)
    assert fenced.spawn(setup.config, "eng", "eng name", TASK) == "placed on the workstation"
    assert setup.store.redis.hkeys(local_slots.key("workstation")) == ["fixture/eng name"]
    assert "fixture: local slot claim eng name for landing on workstation counted 0 of 4" in capsys.readouterr().out
    fenced.runtime.hive_launch.spawn.assert_not_called()


def test_a_workstation_task_whose_claim_finds_the_workstation_full_waits_in_its_lane(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    fenced = hybrid(setup, monkeypatch)
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=4)
    monkeypatch.setattr(local_slots, "claim", lambda *_: 4)
    with pytest.raises(SpawnError, match=placement.LOCAL_FULL):
        fenced.spawn(setup.config, "eng", "eng name", TASK)
    assert placement_waits.read(setup.store, setup.config.slug)["landing"]["lane"] == "eng"
    setup.herdr.spawn.assert_not_called()


def test_a_hybrid_controller_leaves_the_claim_of_a_plain_task_to_the_hive_launch(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    fenced = hybrid(setup, monkeypatch)
    write_hive(setup, id="workstation", ui="yes", excluded=[], max_agents=4)
    assert fenced.spawn(setup.config, "eng", "eng name", {"id": "other"}) == "placed on the hive"
    assert setup.store.redis.hkeys(local_slots.key("workstation")) == []
    setup.herdr.spawn.assert_not_called()


def test_workstation_hives_are_the_headed_hive_else_the_controller_hive() -> None:
    root = {"id": "root", "ui": "yes"}
    anton = {"id": "anton", "ui": "no"}
    own = {"id": "controller", "ui": "no"}
    assert placement.workstation_hives([anton, root, own], "controller") == [root]
    assert placement.workstation_hives([anton, own], "controller") == [own]
    assert placement.workstation_hives([anton], "controller") == []


def test_a_controller_off_the_workstation_selects_the_root_hive_for_a_workstation_task(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "hive_id", lambda: "anton-controller")
    write_hive(setup, prefer={"eng": 0})
    write_hive(setup, id="root", ui="yes", excluded=[], max_agents=20)
    assert setup.launcher.select(setup.config, "eng", TASK).hive == "root"


def test_a_distributed_controller_launches_a_workstation_task_on_the_root_hive(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "hive_id", lambda: "anton-controller")
    fenced = hybrid(setup, monkeypatch, "distributed")
    write_hive(setup, id="root", ui="yes", excluded=[], max_agents=20)
    launcher = fenced.runtime.hive_launch
    select, lanes = launcher.select, []
    monkeypatch.setattr(launcher, "select", lambda config, lane, task: lanes.append(lane) or select(config, lane, task))
    assert fenced.placement_refusal("eng", TASK) == ""
    assert fenced.spawn(setup.config, "eng", "eng name", TASK) == "placed on the hive"
    assert set(lanes) == {"eng"}
    (request,) = fenced.runtime.hive_launch.spawn.call_args.args
    assert (request.task["workstation"], request.task["hive"], request.task["id"]) == (False, "root", "landing")
    assert setup.store.redis.hkeys(local_slots.key("anton-controller")) == []
    setup.herdr.spawn.assert_not_called()
