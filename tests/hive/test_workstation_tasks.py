from scripts.hive import launch, placement, placement_waits
from scripts.hive.placement import Wait
from scripts.swarm import commands
from scripts.swarm_v2.runtime.base import LOCAL, Placement
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
    assert placement_waits.read(setup.store, setup.config.slug)["landing"]["reason"] == placement.NEEDS_WORKSTATION


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
